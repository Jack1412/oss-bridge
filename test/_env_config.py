"""测试用的 OSS 环境配置。

所有私有信息都从环境变量读取，仓库里不出现任何 bucket / endpoint / 账号。
需要的环境变量：

    OSS_BRIDGE_TEST_ENDPOINT          OSS endpoint，例如 oss-cn-hangzhou-internal.aliyuncs.com
    OSS_BRIDGE_TEST_BUCKET            测试用 bucket
    OSS_BRIDGE_TEST_PREFIX            测试用前缀，默认 oss-bridge-selftest/

    凭证二选一：
    OSS_BRIDGE_TEST_OSSUTIL_CONFIG    ossutil 配置文件路径（读 endpoint/AK/SK）
    OSS_BRIDGE_TEST_AK / _SK          直接给 AK/SK

未配置时测试会打印跳过原因并直接成功退出，方便在 CI 里无凭证运行。
"""

from __future__ import annotations

import os

from ossbridge.config import Config, load_config

SKIP_MESSAGE = (
    "跳过：未配置 OSS 测试环境。请设置 OSS_BRIDGE_TEST_ENDPOINT / OSS_BRIDGE_TEST_BUCKET，"
    "并提供 OSS_BRIDGE_TEST_OSSUTIL_CONFIG 或 OSS_BRIDGE_TEST_AK + OSS_BRIDGE_TEST_SK。"
)


def test_env() -> dict | None:
    """读取测试环境；缺关键项时返回 None。"""
    endpoint = os.environ.get("OSS_BRIDGE_TEST_ENDPOINT", "").strip()
    bucket = os.environ.get("OSS_BRIDGE_TEST_BUCKET", "").strip()
    prefix = os.environ.get("OSS_BRIDGE_TEST_PREFIX", "").strip() or "oss-bridge-selftest/"
    ossutil_config = os.environ.get("OSS_BRIDGE_TEST_OSSUTIL_CONFIG", "").strip()
    ak = os.environ.get("OSS_BRIDGE_TEST_AK", "").strip()
    sk = os.environ.get("OSS_BRIDGE_TEST_SK", "").strip()
    if not endpoint or not bucket:
        return None
    if not ossutil_config and not (ak and sk):
        return None
    return {
        "endpoint": endpoint,
        "bucket": bucket,
        "prefix": prefix if prefix.endswith("/") else prefix + "/",
        "ossutil_config": ossutil_config,
        "ak": ak,
        "sk": sk,
    }


def build_test_config(
    env: dict,
    *,
    secret: str = "",
    state_dir: str = "",
    tuning_file: str | None = None,
    no_signature: bool = False,
    extra: dict | None = None,
) -> Config:
    """按测试环境构造配置（与 runner 子进程保持一致的来源顺序）。"""
    overrides: dict = {
        "endpoint": env["endpoint"],
        "bucket": env["bucket"],
        "prefix": env["prefix"],
        "secret": secret or None,
        "require_signature": False if no_signature else None,
        "state_dir": state_dir or None,
    }
    if env["ak"]:
        overrides["access_key_id"] = env["ak"]
        overrides["access_key_secret"] = env["sk"]
    overrides.update(extra or {})
    return load_config(
        config_file=tuning_file,
        ossutil_config=env["ossutil_config"] or None,
        overrides=overrides,
    )


def common_cli_args(env: dict) -> list[str]:
    """生成 runner / MCP server 子进程共用的目标与凭证参数。"""
    args = ["--endpoint", env["endpoint"], "--bucket", env["bucket"], "--prefix", env["prefix"]]
    if env["ossutil_config"]:
        args += ["--ossutil-config", env["ossutil_config"]]
    else:
        args += ["--ak", env["ak"], "--sk", env["sk"]]
    return args


def remote_script_env(env: dict, sim_root: str, secret_file: str, no_signature: bool) -> dict:
    """生成 remote_run.sh 需要的环境变量。"""
    values = {
        "SRC_ROOT": sim_root,
        "OSS_ENDPOINT": env["endpoint"],
        "OSS_BUCKET": env["bucket"],
        "OSS_PREFIX": env["prefix"].rstrip("/"),
    }
    if env["ossutil_config"]:
        values["OSS_BRIDGE_OSSUTIL_CONFIG"] = env["ossutil_config"]
    else:
        values["OSS_BRIDGE_AK"] = env["ak"]
        values["OSS_BRIDGE_SK"] = env["sk"]
    if no_signature:
        values["OSS_BRIDGE_NO_SIGNATURE"] = "1"
    else:
        values["OSS_BRIDGE_SECRET_FILE"] = secret_file
    return values
