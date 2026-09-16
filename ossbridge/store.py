"""OSS 读写封装。

只用四个 API：PutObject / GetObject / ListObjectsV2 / DeleteObject。
PutObject 带 ``x-oss-forbid-overwrite: true`` 时构成原子抢占原语
（对象已存在则返回 409），用来做"谁抢到谁执行"的互斥锁。

注意：bucket 若开启版本控制，该 header 会失效，抢占语义不再可靠。
"""

from __future__ import annotations

import json
import time
from typing import Any

import oss2

from .config import Config


class ObjectExists(RuntimeError):
    """目标对象已存在（forbid-overwrite 抢占失败）。"""


class OssStore:
    """按 ``{prefix}`` 前缀组织的对象存取；所有 key 参数都是相对前缀的路径。"""

    def __init__(self, cfg: Config):
        cfg.require_target()
        cfg.require_credentials()
        self.cfg = cfg
        if cfg.security_token:
            auth: oss2.AuthBase = oss2.StsAuth(
                cfg.access_key_id, cfg.access_key_secret, cfg.security_token
            )
        else:
            auth = oss2.Auth(cfg.access_key_id, cfg.access_key_secret)
        self.bucket = oss2.Bucket(auth, cfg.endpoint, cfg.bucket)

    # ---------- key 组织 ----------

    def key(self, *parts: str) -> str:
        """拼接相对前缀的完整对象 key。"""
        return self.cfg.prefix + "/".join(p.strip("/") for p in parts if p != "")

    def registry_key(self, host_id: str) -> str:
        return self.key("registry", f"{host_id}.json")

    def registry_owner_key(self, host_id: str) -> str:
        return self.key("registry", host_id, "owner.json")

    def inbox_pending(self, host_id: str, name: str) -> str:
        return self.key("hosts", host_id, "inbox", "pending", name)

    def inbox_claimed(self, host_id: str, name: str) -> str:
        return self.key("hosts", host_id, "inbox", "claimed", name)

    def inbox_done(self, host_id: str, name: str) -> str:
        return self.key("hosts", host_id, "inbox", "done", name)

    def outbox_result(self, host_id: str, name: str) -> str:
        return self.key("hosts", host_id, "outbox", "result", name)

    def outbox_progress(self, host_id: str, name: str) -> str:
        return self.key("hosts", host_id, "outbox", "progress", name)

    def file_key(self, request_id: str, name: str) -> str:
        return self.key("files", request_id, name)

    def files_prefix(self, request_id: str) -> str:
        return self.key("files", request_id) + "/"

    def hosts_prefix(self, host_id: str) -> str:
        return self.key("hosts", host_id) + "/"

    # ---------- 基本操作 ----------

    def put_bytes(self, key: str, data: bytes, forbid_overwrite: bool = False) -> str:
        """写入对象并返回 ETag；forbid_overwrite=True 且对象已存在时抛 ObjectExists。"""
        headers = {"x-oss-forbid-overwrite": "true"} if forbid_overwrite else None
        try:
            result = self.bucket.put_object(key, data, headers=headers)
        except oss2.exceptions.ServerError as exc:
            if exc.status == 409:
                raise ObjectExists(key) from exc
            raise
        return result.etag

    def put_json(self, key: str, payload: Any, forbid_overwrite: bool = False) -> str:
        return self.put_bytes(
            key, json.dumps(payload, ensure_ascii=False).encode("utf-8"), forbid_overwrite
        )

    def get_bytes(self, key: str) -> bytes:
        return self.bucket.get_object(key).read()

    def get_json(self, key: str) -> Any:
        return json.loads(self.get_bytes(key).decode("utf-8"))

    def exists(self, key: str) -> bool:
        return bool(self.bucket.object_exists(key))

    def delete(self, key: str) -> None:
        self.bucket.delete_object(key)

    def list_keys(
        self, prefix: str, start_after: str | None = None, max_keys: int = 200
    ) -> list[str]:
        """列举前缀下的对象 key（字典序升序）。

        输入：prefix 相对 key 前缀、start_after 增量游标、max_keys 单次上限。
        输出：完整 key 列表。
        """
        keys: list[str] = []
        token = ""
        max_keys = max(1, min(int(max_keys), 1000))  # OSS 限制单次 max-keys 在 1~1000
        while True:
            result = self.bucket.list_objects_v2(
                prefix=prefix,
                start_after=start_after,
                continuation_token=token,
                max_keys=max_keys,
            )
            keys.extend(obj.key for obj in result.object_list)
            if not result.is_truncated:
                break
            token = result.next_continuation_token
            start_after = None  # 后续分页交给 continuation_token
        return keys

    def upload_file(self, key: str, local_path: str) -> None:
        self.bucket.put_object_from_file(key, local_path)

    def download_file(self, key: str, local_path: str) -> None:
        self.bucket.get_object_to_file(key, local_path)

    def move(self, src_key: str, dst_key: str) -> None:
        """同 bucket 内复制后删除源对象。"""
        self.bucket.copy_object(self.cfg.bucket, src_key, dst_key)
        self.bucket.delete_object(src_key)

    def wait_until(self, key: str, timeout_s: float, interval_s: float = 0.25) -> bool:
        """自旋等待对象出现（本机侧等结果用）。"""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.exists(key):
                return True
            time.sleep(interval_s)
        return self.exists(key)

    def list_json(self, prefix: str, max_keys: int = 200) -> list[dict]:
        """列举前缀下所有 JSON 对象并解析；单个对象损坏不影响整体。"""
        items: list[dict] = []
        for key in self.list_keys(prefix, max_keys=max_keys):
            try:
                payload = self.get_json(key)
            except Exception:
                continue
            if isinstance(payload, dict):
                payload["_key"] = key
                items.append(payload)
        return items
