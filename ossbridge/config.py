"""配置加载。

优先级（从高到低）：命令行参数 > 环境变量 > 配置文件 > 内置默认值。
凭证支持两种形态：
  1. 长期 AK/SK；
  2. STS 临时凭证（access_key_id + access_key_secret + security_token），
     可直接读取 fuyao 注入的 ``/fuyao_oss_sts/token`` 这类 JSON 文件。

注意：``secret`` 是两端共享的 HMAC 密钥，用来给指令签名，防止有人往
bucket 里塞文件就能在远端执行命令。它只存在两端本地配置中，绝不写入 OSS。
"""

from __future__ import annotations

import configparser
import json
import os
import tomllib
import sys
from dataclasses import dataclass, replace
from typing import Any

# 不含任何环境私有默认值：endpoint / bucket 必须显式提供（命令行、配置文件或环境变量）。
# 内网环境建议用 OSS 的 internal endpoint（例如 oss-cn-hangzhou-internal.aliyuncs.com），
# 公网 endpoint 在使用虚拟主机风格域名时依赖通配 DNS，部分容器环境解析不了。
DEFAULT_ENDPOINT = ""
DEFAULT_BUCKET = ""
DEFAULT_PREFIX = "oss-bridge/"


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


@dataclass
class Config:
    """oss-bridge 的全部可配置项。"""

    endpoint: str = DEFAULT_ENDPOINT
    bucket: str = DEFAULT_BUCKET
    prefix: str = DEFAULT_PREFIX
    access_key_id: str = ""
    access_key_secret: str = ""
    security_token: str = ""
    secret: str = ""
    # 是否校验指令签名。关闭后任何能写 inbox 的凭证都能在本机执行命令，仅适合私有目录。
    require_signature: bool = True
    state_dir: str = ""
    work_dir: str = ""
    # 轮询节奏：空闲时退避，发现任务后立刻加快
    poll_interval_idle: float = 2.0
    poll_interval_active: float = 0.3
    full_sweep_interval: float = 30.0
    heartbeat_interval: float = 15.0
    # 抢占后租约余量：租约 = timeout_s + lease_grace_s
    lease_grace_s: float = 120.0
    max_attempts: int = 3
    concurrency: int = 2
    max_output_bytes: int = 65536

    def normalized(self) -> "Config":
        """补齐/规范化派生字段，返回新对象。"""
        prefix = self.prefix if self.prefix.endswith("/") else self.prefix + "/"
        state_dir = self.state_dir or os.path.join(os.path.expanduser("~"), ".oss-bridge")
        work_dir = self.work_dir or os.path.join(state_dir, "work")
        endpoint = self.endpoint
        if endpoint and not endpoint.startswith("http"):
            endpoint = "https://" + endpoint
        return replace(self, prefix=prefix, state_dir=state_dir, work_dir=work_dir, endpoint=endpoint)

    def require_target(self) -> None:
        """校验 OSS 目标（endpoint / bucket）是否齐备。"""
        missing = [name for name, value in (("endpoint", self.endpoint), ("bucket", self.bucket)) if not value]
        if missing:
            raise ConfigError(
                f"缺少 {'、'.join(missing)}：请通过 --endpoint/--bucket、配置文件或"
                "环境变量 OSS_BRIDGE_ENDPOINT / OSS_BRIDGE_BUCKET 提供"
            )

    def require_credentials(self) -> None:
        """校验凭证是否齐备。"""
        if not self.access_key_id or not self.access_key_secret:
            raise ConfigError(
                "缺少 OSS 凭证：请通过 --ossutil-config / --sts-token-file / "
                "--ak --sk / 环境变量 OSS_BRIDGE_AK 提供"
            )

    def require_secret(self) -> None:
        """校验 HMAC 共享密钥是否设置；关闭签名校验时不需要密钥。"""
        if not self.require_signature:
            return
        if not self.secret:
            raise ConfigError(
                "缺少 HMAC 共享密钥（两端必须一致）：请通过 --secret 或环境变量 "
                "OSS_BRIDGE_SECRET 提供。生成方式："
                "python3 -c \"import secrets;print(secrets.token_hex(32))\""
            )


def _load_ossutil_config(path: str) -> dict[str, Any]:
    """读取 ossutil 的配置文件（[Credentials] 段）。"""
    parser = configparser.ConfigParser()
    if not parser.read(path):
        raise ConfigError(f"无法读取 ossutil 配置：{path}")
    if "Credentials" not in parser:
        raise ConfigError(f"ossutil 配置缺少 [Credentials] 段：{path}")
    section = parser["Credentials"]
    out: dict[str, Any] = {}
    if section.get("endpoint"):
        out["endpoint"] = section["endpoint"].strip()
    ak = section.get("accessKeyID") or section.get("accessKeyId")
    sk = section.get("accessKeySecret")
    if ak:
        out["access_key_id"] = ak.strip()
    if sk:
        out["access_key_secret"] = sk.strip()
    return out


def _load_sts_token_file(path: str) -> dict[str, Any]:
    """读取 STS 凭证 JSON（如 /fuyao_oss_sts/token）。"""
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    mapping = {
        "access_key_id": ("access_key_id", "AccessKeyId", "accessKeyID"),
        "access_key_secret": ("access_key_secret", "AccessKeySecret"),
        "security_token": ("security_token", "SecurityToken"),
    }
    out: dict[str, Any] = {}
    for target, candidates in mapping.items():
        for key in candidates:
            if data.get(key):
                out[target] = str(data[key]).strip()
                break
    if not out.get("access_key_id"):
        raise ConfigError(f"STS 凭证文件缺少 access_key_id：{path}")
    return out


def _load_project_config(path: str) -> dict[str, Any]:
    """读取项目自己的 TOML 配置。"""
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    section = data.get("oss_bridge", data)
    if not isinstance(section, dict):
        raise ConfigError(f"配置文件格式不正确：{path}")
    return {key: value for key, value in section.items() if not isinstance(value, dict)}


_ENV_MAP = {
    "OSS_BRIDGE_ENDPOINT": "endpoint",
    "OSS_BRIDGE_BUCKET": "bucket",
    "OSS_BRIDGE_PREFIX": "prefix",
    "OSS_BRIDGE_AK": "access_key_id",
    "OSS_BRIDGE_SK": "access_key_secret",
    "OSS_BRIDGE_STS_TOKEN": "security_token",
    "OSS_BRIDGE_SECRET": "secret",
    "OSS_BRIDGE_STATE_DIR": "state_dir",
    "OSS_BRIDGE_WORK_DIR": "work_dir",
}


def clean_argv(argv: list[str] | None) -> list[str]:
    """过滤掉只含空白的参数。

    多行命令里如果行尾的续行反斜杠后面多了一个空格，shell 会把那个空格当成一个独立参数
    传进来，argparse 会报 "unrecognized arguments:  "（后面看起来是空的）。这里直接忽略，
    避免用户对着一个看不见的空格排查半天。
    """
    source = sys.argv[1:] if argv is None else argv
    filtered = [arg for arg in source if arg.strip()]
    dropped = len(source) - len(filtered)
    if dropped:
        print(f"提示：已忽略 {dropped} 个空白参数（多行命令里 \\ 后可能多了空格）", file=sys.stderr)
    return filtered


def load_config(
    *,
    config_file: str | None = None,
    ossutil_config: str | None = None,
    sts_token_file: str | None = None,
    secret_file: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> Config:
    """按优先级合并各来源，返回规范化后的配置。

    输入：各配置来源路径 + 命令行覆盖项字典。
    输出：Config 实例（已 normalize）。
    """
    data: dict[str, Any] = {}
    sources: dict[str, str] = {}  # 记录每个字段最终来自哪里，便于 --print-config 排查

    def _merge(section: dict[str, Any], origin: str) -> None:
        data.update(section)
        for key in section:
            sources[key] = origin

    if config_file:
        _merge(_load_project_config(config_file), f"配置文件 {config_file}")
    if ossutil_config:
        _merge(_load_ossutil_config(ossutil_config), f"ossutil 配置 {ossutil_config}")
    if sts_token_file:
        _merge(_load_sts_token_file(sts_token_file), f"STS 凭证文件 {sts_token_file}")
    for env_key, field in _ENV_MAP.items():
        value = os.environ.get(env_key)
        if value:
            data[field] = value
            sources[field] = f"环境变量 {env_key}"
    cli_values = {k: v for k, v in (overrides or {}).items() if v is not None}
    data.update(cli_values)
    for field in cli_values:
        sources[field] = "命令行参数"
    if not data.get("secret") and secret_file:
        # 从文件读密钥，避免把密钥写进命令行（ps 可见）或环境变量
        with open(secret_file, "r", encoding="utf-8") as handle:
            data["secret"] = handle.read().strip()
        sources["secret"] = f"密钥文件 {secret_file}"

    numeric_fields = {
        "poll_interval_idle": float,
        "poll_interval_active": float,
        "full_sweep_interval": float,
        "heartbeat_interval": float,
        "lease_grace_s": float,
        "max_attempts": int,
        "concurrency": int,
        "max_output_bytes": int,
    }
    for field, caster in numeric_fields.items():
        if field in data:
            data[field] = caster(data[field])

    known = set(Config.__dataclass_fields__)
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"未知配置项：{sorted(unknown)}")
    cfg = Config(**data).normalized()
    cfg.source_map = sources  # 供 describe() 使用
    return cfg


def _mask(value: str) -> str:
    """把凭证打码后再展示。"""
    if not value:
        return "(未设置)"
    return value[:4] + "…" + value[-2:] if len(value) > 8 else "(已设置)"


def describe(cfg: Config) -> list[str]:
    """生成"最终生效配置"的可读说明（不含密钥明文）。

    输入：load_config 得到的 Config。输出：逐行文本，附带每个字段的来源。
    """
    sources: dict[str, str] = getattr(cfg, "source_map", {})

    def origin(field: str, default: str = "内置默认值") -> str:
        return sources.get(field, default)

    return [
        f"endpoint      = {cfg.endpoint or '(未设置)'}    ← {origin('endpoint')}",
        f"bucket        = {cfg.bucket or '(未设置)'}    ← {origin('bucket')}",
        f"prefix        = {cfg.prefix}    ← {origin('prefix')}",
        f"access key id = {_mask(cfg.access_key_id)}    ← {origin('access_key_id')}",
        f"sts token     = {'有' if cfg.security_token else '无'}    ← {origin('security_token')}",
        f"state dir     = {cfg.state_dir}",
        f"work dir      = {cfg.work_dir}",
        f"签名校验      = {'开启' if cfg.require_signature else '关闭（--insecure-no-signature）'}"
        f"    ← {origin('require_signature')}",
        f"并发/超时     = concurrency={cfg.concurrency} poll_idle={cfg.poll_interval_idle}s "
        f"max_output={cfg.max_output_bytes}B",
    ]
