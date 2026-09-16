#!/usr/bin/env bash
#
# oss-bridge 远程运行脚本：拉取 OSS 上的最新代码并启动 runner。
#
# 典型用法（远程机器上，只需自举一次）：
#     ossutil64 cp oss://<bucket>/<prefix>/src/remote_run.sh ./
#     bash remote_run.sh
# 之后每次只要重新执行这个脚本，就会自动拉取最新代码再启动，方便远程测试。
#
# 目标与常用参数可以写进 $SRC_ROOT/bridge.env（本机文件，不会进仓库），
# 例如：
#     OSS_BUCKET=my-bucket
#     OSS_PREFIX=oss-bridge
#     OSS_ENDPOINT=oss-cn-hangzhou-internal.aliyuncs.com
#     OSS_BRIDGE_NO_SIGNATURE=1
# 显式传入的环境变量优先级高于该文件。
#
# 环境变量（都可选）：
#   OSS_BUCKET / OSS_PREFIX / OSS_ENDPOINT   代码与队列位置
#   SRC_ROOT                                 代码缓存目录（默认 ~/.oss-bridge）
#   OSS_BRIDGE_SECRET_FILE                   HMAC 密钥文件（默认 $SRC_ROOT/secret）
#   OSS_BRIDGE_STS_TOKEN_FILE                STS 凭证 JSON
#   OSS_BRIDGE_OSSUTIL_CONFIG                ossutil 配置（读取 endpoint/AK/SK）
#   OSS_BRIDGE_AK / OSS_BRIDGE_SK            直接给 AK/SK
#   OSS_BRIDGE_STATE_DIR / OSS_BRIDGE_INSTANCE / OSS_BRIDGE_CONCURRENCY
#   OSS_BRIDGE_NO_UPDATE=1                   跳过拉取，直接用本地已解开的代码
#   OSS_BRIDGE_NO_SIGNATURE=1                关闭指令签名校验（私有目录可直接用，不用配密钥）
#
# 额外参数原样传给 runner，例如：bash remote_run.sh --log-level DEBUG --concurrency 4

set -euo pipefail

# 调用方显式传入的值优先于 bridge.env 里的默认值
_ENV_OSS_BUCKET="${OSS_BUCKET:-}"
_ENV_OSS_PREFIX="${OSS_PREFIX:-}"
_ENV_OSS_ENDPOINT="${OSS_ENDPOINT:-}"
SRC_ROOT="${SRC_ROOT:-$HOME/.oss-bridge}"
if [ -f "$SRC_ROOT/bridge.env" ]; then
    # shellcheck disable=SC1090
    . "$SRC_ROOT/bridge.env"
fi
OSS_BUCKET="${_ENV_OSS_BUCKET:-${OSS_BUCKET:-}}"
OSS_PREFIX="${_ENV_OSS_PREFIX:-${OSS_PREFIX:-oss-bridge}}"
OSS_ENDPOINT="${_ENV_OSS_ENDPOINT:-${OSS_ENDPOINT:-}}"
STATE_DIR="${OSS_BRIDGE_STATE_DIR:-$SRC_ROOT/state}"
SECRET_FILE="${OSS_BRIDGE_SECRET_FILE:-$SRC_ROOT/secret}"
INSTANCE="${OSS_BRIDGE_INSTANCE:-}"
CONCURRENCY="${OSS_BRIDGE_CONCURRENCY:-2}"
LOG_LEVEL="${OSS_BRIDGE_LOG_LEVEL:-INFO}"
NO_UPDATE="${OSS_BRIDGE_NO_UPDATE:-0}"
NO_SIGNATURE="${OSS_BRIDGE_NO_SIGNATURE:-0}"
VENDOR_DIR="$SRC_ROOT/vendor"

log() { printf '[oss-bridge] %s\n' "$*" >&2; }
die() { printf '[oss-bridge] 错误：%s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "找不到 python3"
mkdir -p "$SRC_ROOT" "$STATE_DIR"
if [ -z "$OSS_BUCKET" ] || [ -z "$OSS_ENDPOINT" ]; then
    die "缺少 OSS_BUCKET / OSS_ENDPOINT：写进 $SRC_ROOT/bridge.env，或作为环境变量传入"
fi

# ---------- 1. 组装凭证候选（按优先级尝试，第一个能用上的胜出）----------
# 说明：fuyao 注入的 STS 只覆盖部分 bucket，未必有这个桶的权限，
# 所以这里做候选轮询，失败就换下一个。
CANDIDATES=()
if [ -n "${OSS_BRIDGE_STS_TOKEN_FILE:-}" ] && [ -f "${OSS_BRIDGE_STS_TOKEN_FILE}" ]; then
    CANDIDATES+=("sts:${OSS_BRIDGE_STS_TOKEN_FILE}")
fi
if [ -f /fuyao_oss_sts/token ]; then
    CANDIDATES+=("sts:/fuyao_oss_sts/token")
fi
if [ -n "${OSS_BRIDGE_OSSUTIL_CONFIG:-}" ] && [ -f "${OSS_BRIDGE_OSSUTIL_CONFIG}" ]; then
    CANDIDATES+=("ossutil:${OSS_BRIDGE_OSSUTIL_CONFIG}")
fi
if [ -f "$SRC_ROOT/ossutilconfig" ]; then
    CANDIDATES+=("ossutil:$SRC_ROOT/ossutilconfig")
fi
if [ -n "${OSS_BRIDGE_AK:-}" ]; then
    CANDIDATES+=("ak:${OSS_BRIDGE_AK}:${OSS_BRIDGE_SK:-}")
fi
[ "${#CANDIDATES[@]}" -gt 0 ] || die "找不到任何 OSS 凭证（可用 OSS_BRIDGE_STS_TOKEN_FILE / OSS_BRIDGE_OSSUTIL_CONFIG / OSS_BRIDGE_AK）"

# 把候选描述翻译成 runner 的启动参数
creds_args_of() {
    case "$1" in
        sts:*)      printf -- '--sts-token-file\0%s\0' "${1#sts:}" ;;
        ossutil:*)  printf -- '--ossutil-config\0%s\0' "${1#ossutil:}" ;;
        ak:*)       local rest="${1#ak:}"
                    printf -- '--ak\0%s\0--sk\0%s\0' "${rest%%:*}" "${rest#*:}" ;;
        *)          return 1 ;;
    esac
}

# ---------- 2. 保证 oss2 可用 ----------
if ! PYTHONPATH="$VENDOR_DIR" python3 -c "import oss2" >/dev/null 2>&1; then
    log "本地没有 oss2，尝试安装到 $VENDOR_DIR"
    python3 -m pip install --quiet --target "$VENDOR_DIR" oss2 >&2 \
        || die "oss2 安装失败，请手动执行：python3 -m pip install oss2"
fi
export PYTHONPATH="$VENDOR_DIR${PYTHONPATH:+:$PYTHONPATH}"
export OSS_BUCKET OSS_PREFIX OSS_ENDPOINT SRC_ROOT

# ---------- 3. 拉取并解压最新代码 ----------
CURRENT_DIR="$SRC_ROOT/current"
CHOSEN_CRED=""
if [ "$NO_UPDATE" != "1" ]; then
    for candidate in "${CANDIDATES[@]}"; do
        log "尝试用 ${candidate%%:*} 凭证拉取代码"
        if NEW_DIR="$(export CRED_DESC="$candidate"; python3 - <<'PY'
"""下载最新代码包、校验 sha256、解压，并输出代码目录路径。"""
import hashlib
import json
import os
import shutil
import sys
import tarfile

import oss2

endpoint = os.environ["OSS_ENDPOINT"]
if not endpoint.startswith("http"):
    endpoint = "https://" + endpoint
prefix = os.environ["OSS_PREFIX"].strip("/")
src_root = os.environ["SRC_ROOT"]
desc = os.environ["CRED_DESC"]


def build_auth(desc: str):
    """把凭证描述转成 oss2 的 auth 对象。"""
    if desc.startswith("sts:"):
        token = json.load(open(desc[4:], encoding="utf-8"))
        return oss2.StsAuth(
            token["access_key_id"], token["access_key_secret"], token["security_token"]
        )
    if desc.startswith("ossutil:"):
        import configparser

        parser = configparser.ConfigParser()
        parser.read(desc[len("ossutil:"):])
        section = parser["Credentials"]
        return oss2.Auth(section["accessKeyID"], section["accessKeySecret"])
    if desc.startswith("ak:"):
        rest = desc[len("ak:"):]
        return oss2.Auth(rest.split(":", 1)[0], rest.split(":", 1)[1])
    raise SystemExit(f"未知凭证类型：{desc}")


bucket = oss2.Bucket(build_auth(desc), endpoint, os.environ["OSS_BUCKET"])
try:
    metadata = json.loads(bucket.get_object(f"{prefix}/src/latest.json").read().decode("utf-8"))
except Exception as exc:  # 权限不足或对象不存在都会走到这里
    sys.exit(f"读取 latest.json 失败：{exc}")

expected = metadata["sha256"]
digest8 = expected[:8]
target_dir = os.path.join(src_root, "releases", digest8)
if not os.path.isdir(target_dir):
    dl_path = os.path.join(src_root, "dl", "oss-bridge-src.tar.gz")
    os.makedirs(os.path.dirname(dl_path), exist_ok=True)
    bucket.get_object_to_file(f"{prefix}/src/oss-bridge-src.tar.gz", dl_path)
    actual = hashlib.sha256(open(dl_path, "rb").read()).hexdigest()
    if actual != expected:
        sys.exit(f"校验失败：期望 {expected}，实际 {actual}")
    tmp_dir = os.path.join(src_root, "tmp", digest8)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir, exist_ok=True)
    with tarfile.open(dl_path, "r:gz") as tar:
        tar.extractall(tmp_dir)
    os.makedirs(os.path.dirname(target_dir), exist_ok=True)
    shutil.rmtree(target_dir, ignore_errors=True)
    os.replace(tmp_dir, target_dir)
    shutil.rmtree(os.path.join(src_root, "tmp"), ignore_errors=True)
print(target_dir)
PY
)"; then
            CHOSEN_CRED="$candidate"
            break
        fi
        log "该凭证不可用，换下一个"
    done
    [ -n "$CHOSEN_CRED" ] || die "所有凭证都拉不到代码，请检查权限/endpoint/bucket"
    log "代码目录：$NEW_DIR（凭证：${CHOSEN_CRED%%:*}）"
    ln -sfn "$NEW_DIR" "$CURRENT_DIR"
    (ls -1dt "$SRC_ROOT"/releases/*/ 2>/dev/null | tail -n +4 | xargs -r rm -rf) || true
else
    CHOSEN_CRED="${CANDIDATES[0]}"
    log "OSS_BRIDGE_NO_UPDATE=1，跳过代码更新"
fi

[ -d "$CURRENT_DIR/oss-bridge" ] || die "本地没有可用代码，请先不带 OSS_BRIDGE_NO_UPDATE 跑一次"
CODE_DIR="$CURRENT_DIR/oss-bridge"

# ---------- 4. 组装启动参数 ----------
mapfile -d '' -t CREDS_ARGS < <(creds_args_of "$CHOSEN_CRED")
# 必须显式指定 endpoint：ossutil 配置里往往是公网域名，会覆盖默认值，
# 而公网 bucket 域名在 glibc 客户端下解析不了
RUN_ARGS=("${CREDS_ARGS[@]}" \
    --endpoint "$OSS_ENDPOINT" --bucket "$OSS_BUCKET" --prefix "$OSS_PREFIX" \
    --state-dir "$STATE_DIR" --concurrency "$CONCURRENCY" --log-level "$LOG_LEVEL")
[ -n "$INSTANCE" ] && RUN_ARGS+=(--instance "$INSTANCE")
[ -n "${OSS_BRIDGE_HOST_ID:-}" ] && RUN_ARGS+=(--host-id "$OSS_BRIDGE_HOST_ID")
if [ "$NO_SIGNATURE" = "1" ]; then
    RUN_ARGS+=(--insecure-no-signature)
    log "注意：已关闭指令签名校验（任何能写 inbox 的凭证都能在本机执行命令）"
elif [ -f "$SECRET_FILE" ]; then
    RUN_ARGS+=(--secret-file "$SECRET_FILE")
elif [ -n "${OSS_BRIDGE_SECRET:-}" ]; then
    RUN_ARGS+=(--secret "$OSS_BRIDGE_SECRET")
else
    die "缺少 HMAC 密钥：把密钥写入 $SECRET_FILE，或设置 OSS_BRIDGE_SECRET；\n      私有目录也可以设置 OSS_BRIDGE_NO_SIGNATURE=1 直接关掉签名校验"
fi

log "启动 runner（代码目录 $CODE_DIR）"
cd "$CODE_DIR"
export PYTHONPATH="$CODE_DIR:$VENDOR_DIR${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m ossbridge.runner "${RUN_ARGS[@]}" "$@"
