"""把当前代码打包并发布到 OSS，供远程机器拉取运行。

发布产物（都在 ``{prefix}/src/`` 下）：
    oss-bridge-src.tar.gz                    最新代码（覆盖写）
    latest.json                              版本描述：sha256 / 大小 / 时间 / 文件数
    releases/<时间>-<sha8>.tar.gz            历史版本，保留最近 N 个，可回滚
    remote_run.sh                            远程运行脚本（方便先下载再运行）

打包是确定性的：文件排序、mtime/uid/gid 归零，因此代码没变时 sha256 不变，
脚本会跳过重复上传（要强制重传加 ``--force``）。

用法：
    python3 scripts/publish_src.py [--ossutil-config PATH] [--secret ...] [--force]
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import sys
import tarfile
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from ossbridge.config import load_config  # noqa: E402
from ossbridge.store import OssStore  # noqa: E402

# 需要发布的文件（相对项目根目录）；测试一起带上，方便在远端直接自测
INCLUDE_FILES = ["README.md", "README.zh-CN.md", "LICENSE", "pyproject.toml", "requirements.txt"]
INCLUDE_DIRS = ["ossbridge", "test", "scripts", "examples"]
EXCLUDE_SUFFIX = (".pyc",)
EXCLUDE_PARTS = ("__pycache__",)
TOP_DIR = "oss-bridge"
KEEP_RELEASES = 5


def collect_files() -> list[str]:
    """收集要打包的文件列表（相对项目根目录，已排序）。"""
    files: list[str] = []
    for name in INCLUDE_FILES:
        path = os.path.join(PROJECT_ROOT, name)
        if os.path.isfile(path):
            files.append(name)
    for dirname in INCLUDE_DIRS:
        base = os.path.join(PROJECT_ROOT, dirname)
        for root, sub_dirs, names in os.walk(base):
            sub_dirs[:] = [d for d in sub_dirs if d not in EXCLUDE_PARTS]
            for filename in names:
                if filename.endswith(EXCLUDE_SUFFIX):
                    continue
                full = os.path.join(root, filename)
                files.append(os.path.relpath(full, PROJECT_ROOT))
    return sorted(set(files))


def build_tarball(files: list[str]) -> tuple[bytes, str]:
    """生成确定性 tar.gz。

    输入：相对路径列表。输出：(字节内容, sha256 十六进制)。
    """
    buffer = io.BytesIO()
    # gzip 头里的 mtime 必须固定，否则内容相同也会算出不同 sha256
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0, compresslevel=6) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
            _add_files(tar, files)
    data = buffer.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def _add_files(tar: tarfile.TarFile, files: list[str]) -> None:
    """把项目文件按确定性元信息写入 tar。"""
    for rel in files:
        info = tar.gettarinfo(os.path.join(PROJECT_ROOT, rel), arcname=f"{TOP_DIR}/{rel}")
        info.mtime = 0
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mode = 0o755 if rel.endswith((".sh", ".py")) else 0o644
        with open(os.path.join(PROJECT_ROOT, rel), "rb") as handle:
            tar.addfile(info, handle)


def prune_releases(store: OssStore, prefix: str, keep: int) -> int:
    """只保留最近的 N 个历史版本，返回删除数量。"""
    keys = sorted(store.list_keys(prefix, max_keys=1000))
    for key in keys[:-keep] if len(keys) > keep else []:
        store.delete(key)
    return max(0, len(keys) - keep)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打包并发布 oss-bridge 代码到 OSS")
    parser.add_argument("--config")
    parser.add_argument("--ossutil-config")
    parser.add_argument("--sts-token-file")
    parser.add_argument("--endpoint")
    parser.add_argument("--bucket")
    parser.add_argument("--prefix")
    parser.add_argument("--ak")
    parser.add_argument("--sk")
    parser.add_argument("--secret", default="unused", help="发布不需要签名密钥，占位即可")
    parser.add_argument("--force", action="store_true", help="内容没变也重新上传")
    args = parser.parse_args(argv)

    cfg = load_config(
        config_file=args.config,
        ossutil_config=args.ossutil_config,
        sts_token_file=args.sts_token_file,
        overrides={
            "endpoint": args.endpoint,
            "bucket": args.bucket,
            "prefix": args.prefix,
            "access_key_id": args.ak,
            "access_key_secret": args.sk,
        },
    )
    store = OssStore(cfg)
    source_prefix = store.key("src") + "/"
    files = collect_files()
    data, digest = build_tarball(files)
    latest_key = source_prefix + "latest.json"

    previous = None
    if store.exists(latest_key):
        try:
            previous = store.get_json(latest_key)
        except Exception:
            previous = None
    if previous and previous.get("sha256") == digest and not args.force:
        print(f"内容与线上一致（sha256={digest[:12]}），跳过上传。要强制上传加 --force")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    package_key = source_prefix + "oss-bridge-src.tar.gz"
    release_key = f"{source_prefix}releases/{stamp}-{digest[:8]}.tar.gz"
    metadata = {
        "sha256": digest,
        "size": len(data),
        "file_count": len(files),
        "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "package": package_key,
        "release": release_key,
        "files": files,
    }
    store.put_bytes(package_key, data)
    store.put_bytes(release_key, data)
    store.put_json(latest_key, metadata)

    # 顺便把远程运行脚本单独放一份，方便远程机器一条命令自举
    remote_script = os.path.join(PROJECT_ROOT, "scripts", "remote_run.sh")
    if os.path.isfile(remote_script):
        with open(remote_script, "rb") as handle:
            store.put_bytes(source_prefix + "remote_run.sh", handle.read())

    removed = prune_releases(store, source_prefix + "releases/", KEEP_RELEASES)
    print(
        f"已发布 {len(files)} 个文件，{len(data)} 字节\n"
        f"  sha256   : {digest}\n"
        f"  最新包   : oss://{cfg.bucket}/{package_key}\n"
        f"  历史版本 : oss://{cfg.bucket}/{release_key}（已清理 {removed} 个旧版本）\n"
        f"  元数据   : oss://{cfg.bucket}/{latest_key}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
