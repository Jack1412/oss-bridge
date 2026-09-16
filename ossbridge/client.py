"""本机侧客户端：把指令写进 OSS、等结果、搬文件。

MCP server 和自测脚本都复用这一层，避免两处实现不一致。
"""

from __future__ import annotations

import os
import time
from typing import Any

from .config import Config
from .protocol import Request, new_request_id, object_name, wrap
from .store import OssStore


class HostNotResolved(RuntimeError):
    """host 参数无法唯一确定一台机器。"""


class BridgeClient:
    """本机侧（agent 所在机器）的 OSS 交互客户端。"""

    def __init__(self, cfg: Config):
        cfg.require_secret()
        self.cfg = cfg
        self.store = OssStore(cfg)

    # ---------- 机器发现 ----------

    def hosts(self) -> list[dict]:
        """列出注册表里的机器。

        输出：按 host_id 排序的列表，每项含 host_id / hostname / online / runtime 等。
        """
        prefix = self.store.key("registry")
        now = time.time()
        items: list[dict] = []
        for payload in self.store.list_json(prefix):
            key = payload.pop("_key", "")
            if key.endswith("/owner.json"):
                continue  # owner 不是心跳对象
            heartbeat_at = float(payload.get("heartbeat_at", 0) or 0)
            lease = float(payload.get("lease_expire_at", 0) or 0)
            runtime = payload.get("runtime") or {}
            items.append(
                {
                    "host_id": payload.get("host_id"),
                    "hostname": payload.get("hostname"),
                    "online": lease > now,
                    "last_heartbeat_s": round(now - heartbeat_at, 1) if heartbeat_at else None,
                    "in_flight": payload.get("in_flight", 0),
                    "version": payload.get("version"),
                    "gpu": [g.get("name") for g in runtime.get("gpu", [])],
                    "gpu_count": len(runtime.get("gpu", [])),
                    "loadavg": runtime.get("loadavg"),
                    "cwd": runtime.get("cwd"),
                }
            )
        return sorted(items, key=lambda item: str(item.get("host_id")))

    def resolve_host(self, host: str) -> str:
        """把用户给的 host 参数解析成唯一 host_id。

        支持：完整 host_id、hostname、二者的唯一前缀。
        输出：host_id；无法唯一匹配时抛 HostNotResolved 并给出候选。
        """
        if not host:
            raise HostNotResolved("必须指定 host")
        known = self.hosts()
        ids = [str(item["host_id"]) for item in known]
        if host in ids:
            return host
        by_hostname = [str(item["host_id"]) for item in known if item.get("hostname") == host]
        if len(by_hostname) == 1:
            return by_hostname[0]
        prefixed = [
            str(item["host_id"])
            for item in known
            if str(item["host_id"]).startswith(host) or str(item.get("hostname") or "").startswith(host)
        ]
        if len(prefixed) == 1:
            return prefixed[0]
        if not prefixed:
            raise HostNotResolved(
                f"找不到匹配 {host!r} 的机器；当前在线的有：{ids or '（注册表为空）'}"
            )
        raise HostNotResolved(f"{host!r} 匹配到多台机器，请写全：{prefixed}")

    # ---------- 提交与等待 ----------

    def _submit(self, req: Request) -> str:
        """把指令写进 pending，返回对象名。"""
        name = object_name(req.id)
        self.store.put_bytes(
            self.store.inbox_pending(req.host_id, name),
            wrap(
                req.to_payload(),
                self.cfg.secret,
                "oss-bridge/request@1",
                sign_request=self.cfg.require_signature,
            ),
        )
        return name

    def result(self, host_id: str, name: str) -> dict | None:
        """读取结果对象；未完成返回 None，此时若存在进度则附带进度信息。"""
        key = self.store.outbox_result(host_id, name)
        if self.store.exists(key):
            return self.store.get_json(key)
        return None

    def progress(self, host_id: str, name: str) -> dict | None:
        key = self.store.outbox_progress(host_id, name)
        try:
            return self.store.get_json(key)
        except Exception:
            return None

    def wait(self, host_id: str, name: str, wait_s: float, interval_s: float = 0.25) -> dict | None:
        """轮询等待结果，超时返回 None。"""
        deadline = time.monotonic() + max(0.0, wait_s)
        key = self.store.outbox_result(host_id, name)
        while True:
            if self.store.exists(key):
                return self.store.get_json(key)
            if time.monotonic() >= deadline:
                return None
            time.sleep(interval_s)

    def _finish(self, host_id: str, name: str, request_id: str, wait_s: float) -> dict:
        """统一组装返回给上层的结构。"""
        payload = self.wait(host_id, name, wait_s)
        if payload is None:
            out: dict[str, Any] = {
                "job_id": request_id,
                "object_name": name,
                "host_id": host_id,
                "status": "pending",
                "hint": (
                    f"任务已提交但 {wait_s} 秒内未完成；稍后用 remote_status("
                    f"host={host_id!r}, job_id={request_id!r}) 查询"
                ),
            }
            prog = self.progress(host_id, name)
            if prog:
                out["progress"] = prog
            return out
        result = dict(payload)
        result["job_id"] = result.get("id", request_id)
        result["object_name"] = name
        return result

    # ---------- 业务接口 ----------

    def run(
        self,
        host: str,
        cmd: str = "",
        argv: list[str] | None = None,
        cwd: str = "",
        env: dict[str, str] | None = None,
        timeout_s: float = 600.0,
        wait_s: float = 20.0,
        max_output_bytes: int | None = None,
    ) -> dict:
        """在远端执行命令。输入 host/cmd；输出结果或 pending 句柄。"""
        host_id = self.resolve_host(host)
        req = Request(
            id=new_request_id(),
            host_id=host_id,
            op="exec",
            created_at=time.time(),
            cmd=cmd or "",
            argv=list(argv or []),
            cwd=cwd or "",
            env=dict(env or {}),
            timeout_s=float(timeout_s),
            max_output_bytes=int(max_output_bytes or self.cfg.max_output_bytes),
        )
        name = self._submit(req)
        return self._finish(host_id, name, req.id, wait_s)

    def upload(
        self,
        host: str,
        local_path: str,
        remote_path: str,
        timeout_s: float = 600.0,
        wait_s: float = 30.0,
    ) -> dict:
        """本机文件 → 远端路径：先传 OSS，再让 runner 落盘。"""
        host_id = self.resolve_host(host)
        if not os.path.isfile(local_path):
            raise FileNotFoundError(f"本机文件不存在：{local_path}")
        request_id = new_request_id()
        object_key = self.store.file_key(request_id, os.path.basename(local_path))
        self.store.upload_file(object_key, local_path)
        req = Request(
            id=request_id,
            host_id=host_id,
            op="put",
            created_at=time.time(),
            timeout_s=float(timeout_s),
            uploads=[{"object": object_key, "path": remote_path}],
        )
        name = self._submit(req)
        return self._finish(host_id, name, request_id, wait_s)

    def download(
        self,
        host: str,
        remote_path: str,
        local_path: str,
        timeout_s: float = 600.0,
        wait_s: float = 30.0,
    ) -> dict:
        """远端文件 → 本机路径：让 runner 传 OSS，本机再下载。"""
        host_id = self.resolve_host(host)
        request_id = new_request_id()
        object_key = self.store.file_key(request_id, os.path.basename(remote_path))
        req = Request(
            id=request_id,
            host_id=host_id,
            op="get",
            created_at=time.time(),
            timeout_s=float(timeout_s),
            downloads=[{"path": remote_path, "object": object_key}],
        )
        name = self._submit(req)
        result = self._finish(host_id, name, request_id, wait_s)
        if result.get("status") == "ok":
            os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
            self.store.download_file(object_key, local_path)
            result["local_path"] = local_path
            result["size"] = os.path.getsize(local_path)
            result["object"] = object_key
        return result

    def status(self, host: str, job_id: str) -> dict:
        """查询任务状态；需要 name（对象名）才能定位结果，故这里按 job_id 反查目录。"""
        host_id = self.resolve_host(host)
        prefix = self.store.key("hosts", host_id, "outbox", "result") + "/"
        for key in self.store.list_keys(prefix, max_keys=1000):
            if job_id[:12] in key:
                payload = self.store.get_json(key)
                payload["job_id"] = payload.get("id", job_id)
                payload["object_name"] = key.rsplit("/", 1)[-1]
                return payload
        pending_prefix = self.store.key("hosts", host_id, "inbox", "pending") + "/"
        for key in self.store.list_keys(pending_prefix, max_keys=1000):
            if job_id[:12] in key:
                name = key.rsplit("/", 1)[-1]
                out: dict[str, Any] = {
                    "job_id": job_id,
                    "host_id": host_id,
                    "object_name": name,
                    "status": "pending",
                }
                prog = self.progress(host_id, name)
                if prog:
                    out["progress"] = prog
                return out
        return {"job_id": job_id, "host_id": host_id, "status": "unknown", "hint": "未找到该 job_id"}

    def fetch_output(
        self, host: str, job_id: str, stream: str = "stdout", max_bytes: int = 65536
    ) -> dict:
        """从 OSS 取回被截断的完整输出（取尾部 max_bytes）。"""
        host_id = self.resolve_host(host)
        result = self.status(host, job_id)
        key_field = f"{stream}_object"
        object_key = result.get(key_field) or ""
        if not object_key:
            text = result.get(stream) or ""
            return {"job_id": job_id, "stream": stream, "text": text, "truncated": False}
        raw = self.store.get_bytes(object_key)
        if len(raw) <= max_bytes:
            return {"job_id": job_id, "stream": stream, "text": raw.decode("utf-8", "replace"), "truncated": False}
        tail = raw[-max_bytes:]
        return {
            "job_id": job_id,
            "stream": stream,
            "text": tail.decode("utf-8", "replace"),
            "truncated": True,
            "total_bytes": len(raw),
            "object": object_key,
        }
