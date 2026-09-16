"""指令/结果的消息格式与签名。

消息在 OSS 上是一个 JSON 信封：
    {"kind": "oss-bridge/request@1", "payload": {...}, "sig": "<hex>"}
其中 sig = HMAC-SHA256(secret, canonical_json(payload))。
远端 runner 验签失败一律拒绝执行——这是"能写 bucket 不等于能执行命令"的唯一防线。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import shlex
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

REQUEST_KIND = "oss-bridge/request@1"
RESULT_KIND = "oss-bridge/result@1"

# 允许的指令时间偏移（秒），超出即视为重放或时钟严重不同步
MAX_CLOCK_SKEW_S = 900


class SignatureError(RuntimeError):
    """签名校验失败。"""


def canonical_bytes(payload: Any) -> bytes:
    """生成可复现的字节串（排序 key、紧凑分隔符），用于签名。"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def sign(payload: Any, secret: str) -> str:
    """对 payload 计算 HMAC-SHA256 签名。"""
    return hmac.new(secret.encode("utf-8"), canonical_bytes(payload), hashlib.sha256).hexdigest()


def verify(payload: Any, signature: str, secret: str) -> None:
    """校验签名，失败抛 SignatureError。"""
    if not hmac.compare_digest(sign(payload, secret), signature or ""):
        raise SignatureError("签名不匹配")


def new_request_id() -> str:
    return uuid.uuid4().hex


def object_name(request_id: str) -> str:
    """生成按时间递增排序的指令对象名，便于 runner 用 start_after 增量拉取。"""
    return f"{int(time.time() * 1000):013d}-{request_id[:12]}.json"


def clock_ok(created_at: float, now: float | None = None) -> bool:
    """判断指令时间是否在允许窗口内（防重放）。"""
    now = time.time() if now is None else now
    return abs(now - float(created_at or 0)) <= MAX_CLOCK_SKEW_S


@dataclass
class Request:
    """一条指令。

    op 取值：
      * exec：在远端执行命令
      * put ：把 OSS 上的文件落到远端指定路径
      * get ：把远端文件上传到 OSS
    """

    id: str
    host_id: str
    op: str
    created_at: float
    argv: list[str] = field(default_factory=list)
    cmd: str = ""
    cwd: str = ""
    env: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 600.0
    max_output_bytes: int = 65536
    uploads: list[dict] = field(default_factory=list)
    downloads: list[dict] = field(default_factory=list)
    attempt: int = 0

    def to_payload(self) -> dict:
        return {
            "id": self.id,
            "host_id": self.host_id,
            "op": self.op,
            "created_at": self.created_at,
            "argv": self.argv,
            "cmd": self.cmd,
            "cwd": self.cwd,
            "env": self.env,
            "timeout_s": self.timeout_s,
            "max_output_bytes": self.max_output_bytes,
            "uploads": self.uploads,
            "downloads": self.downloads,
            "attempt": self.attempt,
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "Request":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in payload.items() if k in known})

    def command(self) -> str:
        """返回可直接交给 shell 的完整命令字符串。"""
        if self.cmd:
            return self.cmd
        return " ".join(shlex.quote(part) for part in self.argv)


@dataclass
class Result:
    """一条指令的执行结果。status: ok / failed / timeout / rejected。"""

    id: str
    host_id: str
    status: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_object: str = ""
    stderr_object: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    artifacts: list[dict] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0
    duration_ms: int = 0
    runner_id: str = ""
    error: str = ""

    def to_payload(self) -> dict:
        return {
            "id": self.id,
            "host_id": self.host_id,
            "status": self.status,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_object": self.stdout_object,
            "stderr_object": self.stderr_object,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "artifacts": self.artifacts,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "runner_id": self.runner_id,
            "error": self.error,
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "Result":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in payload.items() if k in known})


def wrap(payload: dict, secret: str, kind: str, sign_request: bool = True) -> bytes:
    """打包成信封字节串；sign_request=False 时不做签名（sig 为空串）。"""
    signature = sign(payload, secret) if sign_request else ""
    envelope = {"kind": kind, "payload": payload, "sig": signature}
    return json.dumps(envelope, ensure_ascii=False).encode("utf-8")


def unwrap(
    raw: bytes, secret: str, expected_kind: str, require_signature: bool = True
) -> dict:
    """解包（可选验签），返回 payload；失败抛异常说明拒绝原因。

    输入：raw 信封字节串、secret 签名密钥、expected_kind 期望消息类型、
    require_signature=False 时跳过签名校验（调用方需自行保证 inbox 的写入权限可信）。
    """
    envelope = json.loads(raw.decode("utf-8"))
    if envelope.get("kind") != expected_kind:
        raise SignatureError(f"消息类型不符：{envelope.get('kind')} != {expected_kind}")
    payload = envelope.get("payload") or {}
    if require_signature:
        verify(payload, envelope.get("sig", ""), secret)
    return payload
