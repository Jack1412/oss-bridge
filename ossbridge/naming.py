"""机器身份（host_id）推导、持久化与运行时信息采集。

设计要点：
  * host_id 首次生成后持久化到 state_dir/host_id，重启保持不变——
    运行中改名会让 inbox 里已提交但未执行的任务永久孤儿化，所以坚决不做。
  * host_id = sanitize(hostname) + "-" + sha1(machine_id|hostname|mac)[:6]，
    hostname 前缀保证人可读，哈希尾巴保证两台同名机器不会撞车。
  * 机器信息（主机名/GPU/负载/版本）写进 registry 心跳，本机 agent 从 registry
    发现机器，并支持用 hostname 或唯一前缀定位 host_id，因此不需要改名。
"""

from __future__ import annotations

import hashlib
import os
import re
import socket
import time
import uuid

_SAFE_CHARS = re.compile(r"[^a-z0-9._-]+")


def now_utc_ms() -> int:
    """当前 UTC 毫秒时间戳（仅用于对象名排序）。"""
    return int(time.time() * 1000)


def sanitize_id(raw: str, max_len: int = 40) -> str:
    """把任意字符串转成可安全用于 OSS key 的标识。"""
    cleaned = _SAFE_CHARS.sub("-", (raw or "").strip().lower()).strip("-")
    return (cleaned or "unknown")[:max_len]


def machine_info() -> dict:
    """采集本机身份信息。

    输出：hostname / machine_id / mac / fingerprint / fingerprint_short。
    """
    hostname = socket.gethostname() or "unknown-host"
    machine_id = ""
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                machine_id = handle.read().strip()
            if machine_id:
                break
        except OSError:
            continue
    mac = "%012x" % uuid.getnode()
    fingerprint = hashlib.sha1(f"{machine_id}|{hostname}|{mac}".encode()).hexdigest()
    return {
        "hostname": hostname,
        "machine_id": machine_id,
        "mac": mac,
        "fingerprint": fingerprint,
        "fingerprint_short": fingerprint[:6],
    }


def derive_host_id(info: dict, instance: str | None = None) -> str:
    """由机器信息推导默认 host_id；主机名不可用时回退为 runner-<随机>。"""
    hostname = sanitize_id(info.get("hostname", ""), max_len=32)
    if not hostname or hostname == "unknown":
        hostname = "runner"
    suffix = info.get("fingerprint_short") or uuid.uuid4().hex[:6]
    host_id = f"{hostname}-{suffix}"
    if instance:
        host_id = f"{host_id}__{sanitize_id(instance, max_len=16)}"
    return host_id


def _host_id_file(state_dir: str) -> str:
    return os.path.join(state_dir, "host_id")


def _atomic_write(path: str, content: str) -> None:
    """先写临时文件再改名，避免读到半截内容。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.replace(tmp, path)


def read_host_id(state_dir: str) -> str | None:
    """读取已持久化的 host_id，不存在返回 None。"""
    try:
        with open(_host_id_file(state_dir), "r", encoding="utf-8") as handle:
            value = handle.read().strip()
        return value or None
    except OSError:
        return None


def write_host_id(state_dir: str, host_id: str) -> None:
    """持久化 host_id，保证重启后任务路径不变。"""
    _atomic_write(_host_id_file(state_dir), host_id + "\n")


def load_or_create_host_id(
    state_dir: str, override: str | None = None, instance: str | None = None
) -> tuple[str, dict]:
    """取得本机 host_id。

    输入：state_dir 持久化目录、override 显式指定、instance 同机多实例区分。
    输出：(host_id, machine_info)。
    """
    info = machine_info()
    if override:
        host_id = sanitize_id(override)
        if instance:
            host_id = f"{host_id}__{sanitize_id(instance, max_len=16)}"
        write_host_id(state_dir, host_id)
        return host_id, info
    existing = read_host_id(state_dir)
    if existing:
        if instance:
            existing = f"{existing.split('__')[0]}__{sanitize_id(instance, max_len=16)}"
        return existing, info
    host_id = derive_host_id(info, instance)
    write_host_id(state_dir, host_id)
    return host_id, info


def collect_runtime_status() -> dict:
    """采集 runner 运行时状态，用于心跳上报。"""
    status: dict = {"pid": os.getpid()}
    try:
        status["loadavg"] = list(os.getloadavg())
    except OSError:
        status["loadavg"] = []
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    status["mem_total_kb"] = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    status["mem_available_kb"] = int(line.split()[1])
    except OSError:
        pass
    status["gpu"] = collect_gpu()
    status["cwd"] = os.getcwd()
    return status


def collect_gpu() -> list[dict]:
    """采集 GPU 信息；没有 nvidia-smi 时返回空列表。"""
    import subprocess

    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.total,memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    gpus = []
    for line in proc.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        gpus.append(
            {
                "index": parts[0],
                "name": parts[1],
                "mem_total_mb": parts[2],
                "mem_used_mb": parts[3],
                "util_percent": parts[4],
            }
        )
    return gpus
