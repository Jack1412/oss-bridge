"""远端守护进程：轮询 OSS、抢占指令、执行、回写结果。

一次完整的执行流程：
    1. LIST hosts/<host_id>/inbox/pending/ 发现新指令（增量游标 + 周期性全量兜底）
    2. GetObject 取指令 → HMAC 验签 → 校验时间戳与目标机器
    3. PutObject(inbox/claimed/<name>, forbid-overwrite=True) 原子抢占，抢不到就放弃
    4. 线程池执行（exec/put/get），长任务定期写 progress
    5. PutObject(outbox/result/<name>) 回写结果 → 写 done 标记 → 删除 pending/claimed

异常与恢复：
  * runner 崩溃时 claimed 对象的租约（timeout + lease_grace）到期后会被其他 runner
    或重启后的自己重新投递（attempt+1），超过 max_attempts 进死信（写 failed 结果）。
  * 结果对象一旦存在，对应 pending 会被直接清理，保证同一 request_id 不会重复执行。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import __version__, naming, protocol
from .config import Config, clean_argv, describe, load_config
from .protocol import Request, Result, SignatureError
from .store import ObjectExists, OssStore

LOG = logging.getLogger("ossbridge.runner")


class Runner:
    """常驻 runner。

    输入：Config、可选的 host_id 覆盖值、实例名。
    输出：无（通过 OSS 交换指令与结果）。
    """

    def __init__(self, cfg: Config, host_id: str | None = None, instance: str | None = None):
        cfg.require_secret()
        self.cfg = cfg
        self.store = OssStore(cfg)
        self.host_id, self.machine = naming.load_or_create_host_id(
            cfg.state_dir, override=host_id, instance=instance
        )
        self.runner_id = uuid.uuid4().hex[:12]
        self.executor = ThreadPoolExecutor(
            max_workers=max(1, cfg.concurrency), thread_name_prefix="ossbridge-exec"
        )
        self._stop = threading.Event()
        self._last_key: str | None = None
        self._last_full_sweep = 0.0
        self._owner_lease_s = 90.0
        self._started_at = time.time()
        self._in_flight = 0
        os.makedirs(cfg.work_dir, exist_ok=True)

    # ---------- 注册与心跳 ----------

    def _owner_payload(self, host_id: str) -> dict:
        return {
            "host_id": host_id,
            "runner_id": self.runner_id,
            "hostname": self.machine["hostname"],
            "machine_fingerprint": self.machine["fingerprint"],
            "version": __version__,
            "started_at": time.time(),
            "lease_expire_at": time.time() + self._owner_lease_s,
        }

    def claim_identity(self) -> str:
        """注册本机身份；若 host_id 已被别的机器占用则自动退让为 -2、-3……

        输出：最终生效的 host_id（同时持久化，重启后复用）。
        """
        base = self.host_id
        for attempt in range(5):
            candidate = base if attempt == 0 else f"{base}-{attempt + 1}"
            owner_key = self.store.registry_owner_key(candidate)
            payload = self._owner_payload(candidate)
            try:
                self.store.put_json(owner_key, payload, forbid_overwrite=True)
            except ObjectExists:
                try:
                    owner = self.store.get_json(owner_key)
                except Exception:
                    owner = {}
                same_machine = owner.get("machine_fingerprint") == self.machine["fingerprint"]
                lease_expired = float(owner.get("lease_expire_at", 0)) < time.time()
                if same_machine or lease_expired:
                    # 同一台机器重启，或原持有者租约已过期：直接接管
                    self.store.put_json(owner_key, payload)
                else:
                    LOG.warning("host_id %s 已被 %s 占用，尝试退让", candidate, owner.get("hostname"))
                    continue
            self.host_id = candidate
            naming.write_host_id(self.cfg.state_dir, candidate)
            LOG.info("已注册 host_id=%s（runner_id=%s）", candidate, self.runner_id)
            return candidate
        raise RuntimeError(f"host_id 连续冲突，无法注册：{base}")

    def heartbeat_once(self) -> None:
        """写一次心跳（registry/<host_id>.json）并续约 owner。"""
        now = time.time()
        payload = {
            "host_id": self.host_id,
            "runner_id": self.runner_id,
            "version": __version__,
            "hostname": self.machine["hostname"],
            "machine_fingerprint": self.machine["fingerprint"],
            "machine_id": self.machine["machine_id"],
            "started_at": self._started_at,
            "heartbeat_at": now,
            "lease_expire_at": now + self.cfg.heartbeat_interval * 3,
            "in_flight": self._in_flight,
            "config": {
                "concurrency": self.cfg.concurrency,
                "bucket": self.cfg.bucket,
                "prefix": self.cfg.prefix,
            },
            "runtime": naming.collect_runtime_status(),
        }
        self.store.put_json(self.store.registry_key(self.host_id), payload)
        self.store.put_json(self.store.registry_owner_key(self.host_id), self._owner_payload(self.host_id))

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.heartbeat_once()
            except Exception as exc:  # 心跳失败不致命
                LOG.warning("心跳失败：%s", exc)
            self._stop.wait(self.cfg.heartbeat_interval)

    # ---------- 轮询与抢占 ----------

    def _pending_prefix(self) -> str:
        return self.store.key("hosts", self.host_id, "inbox", "pending") + "/"

    def _claimed_prefix(self) -> str:
        return self.store.key("hosts", self.host_id, "inbox", "claimed") + "/"

    def poll_once(self) -> int:
        """做一轮轮询与派发。

        输出：本轮发现并派发的指令条数。
        """
        keys = self.store.list_keys(self._pending_prefix(), start_after=self._last_key, max_keys=100)
        if not keys and time.time() - self._last_full_sweep > self.cfg.full_sweep_interval:
            # 全量兜底：防止指令名排序在游标之前导致漏读
            keys = self.store.list_keys(self._pending_prefix(), max_keys=100)
            self._last_full_sweep = time.time()
            self._recover_stale_claims()
        for key in keys:
            self._last_key = key if self._last_key is None else max(self._last_key, key)
            self._dispatch(key)
        return len(keys)

    def _dispatch(self, key: str) -> None:
        """读取并抢占一条指令，抢到后交给线程池执行。"""
        name = key.rsplit("/", 1)[-1]
        result_key = self.store.outbox_result(self.host_id, name)
        if self.store.exists(result_key):
            LOG.info("指令 %s 已有结果，清理 pending", name)
            self._safe_delete(key)
            return
        try:
            raw = self.store.get_bytes(key)
        except Exception as exc:
            LOG.warning("读取指令 %s 失败：%s", name, exc)
            return
        try:
            payload = protocol.unwrap(
                raw,
                self.cfg.secret,
                protocol.REQUEST_KIND,
                require_signature=self.cfg.require_signature,
            )
            req = Request.from_payload(payload)
        except Exception as exc:  # 验签或格式失败一律拒绝
            LOG.error("指令 %s 被拒绝：%s", name, exc)
            self._write_result(
                name,
                Result(
                    id=name[:12],
                    host_id=self.host_id,
                    status="rejected",
                    error=f"验签/格式校验失败：{exc}",
                    runner_id=self.runner_id,
                ),
            )
            return
        if req.host_id and req.host_id != self.host_id:
            LOG.error("指令 %s 目标机器为 %s，本机是 %s，拒绝", name, req.host_id, self.host_id)
            self._write_result(
                name,
                Result(
                    id=req.id,
                    host_id=self.host_id,
                    status="rejected",
                    error=f"目标机器不匹配：{req.host_id}",
                    runner_id=self.runner_id,
                ),
            )
            return
        if not protocol.clock_ok(req.created_at):
            LOG.error("指令 %s 时间戳超出窗口，拒绝", name)
            self._write_result(
                name,
                Result(
                    id=req.id,
                    host_id=self.host_id,
                    status="rejected",
                    error="时间戳超出允许窗口（可能是重放）",
                    runner_id=self.runner_id,
                ),
            )
            return

        claim_key = self.store.inbox_claimed(self.host_id, name)
        claim = {
            "request_id": req.id,
            "object_name": name,
            "runner_id": self.runner_id,
            "host_id": self.host_id,
            "claimed_at": time.time(),
            "lease_expire_at": time.time() + float(req.timeout_s or 0) + self.cfg.lease_grace_s,
            "attempt": int(req.attempt) + 1,
        }
        try:
            self.store.put_json(claim_key, claim, forbid_overwrite=True)
        except ObjectExists:
            LOG.info("指令 %s 已被抢占，跳过", name)
            return
        self._in_flight += 1
        self.executor.submit(self._execute_guarded, req, name)

    def _recover_stale_claims(self) -> None:
        """扫描 claimed 对象：租约过期的重新投递，已有结果的清理掉。"""
        for key in self.store.list_keys(self._claimed_prefix(), max_keys=200):
            name = key.rsplit("/", 1)[-1]
            result_key = self.store.outbox_result(self.host_id, name)
            if self.store.exists(result_key):
                self._safe_delete(key)
                continue
            try:
                claim = self.store.get_json(key)
            except Exception:
                continue
            if float(claim.get("lease_expire_at", 0)) > time.time():
                continue
            attempt = int(claim.get("attempt", 1))
            pending_key = self.store.inbox_pending(self.host_id, name)
            if attempt >= self.cfg.max_attempts:
                LOG.error("指令 %s 超过最大重试次数，写入死信", name)
                self._write_result(
                    name,
                    Result(
                        id=str(claim.get("request_id", name)),
                        host_id=self.host_id,
                        status="failed",
                        error=f"执行租约连续 {attempt} 次过期，放弃重试",
                        runner_id=self.runner_id,
                    ),
                )
                self._safe_delete(pending_key)
                self._safe_delete(key)
                continue
            try:
                raw = self.store.get_bytes(pending_key)
            except Exception:
                # pending 已丢失，无法重投
                self._safe_delete(key)
                continue
            payload = protocol.unwrap(
                raw,
                self.cfg.secret,
                protocol.REQUEST_KIND,
                require_signature=self.cfg.require_signature,
            )
            payload["attempt"] = attempt
            retry_name = protocol.object_name(str(payload.get("id", name)))
            LOG.warning("指令 %s 租约过期，重新投递为 %s（attempt=%s）", name, retry_name, attempt)
            self.store.put_bytes(
                self.store.inbox_pending(self.host_id, retry_name),
                protocol.wrap(payload, self.cfg.secret, protocol.REQUEST_KIND),
            )
            self._safe_delete(pending_key)
            self._safe_delete(key)

    # ---------- 执行 ----------

    def _execute_guarded(self, req: Request, name: str) -> None:
        """线程池入口：保证异常不逃逸，并回写结果。"""
        started = time.time()
        result = Result(
            id=req.id,
            host_id=self.host_id,
            status="ok",
            started_at=started,
            runner_id=self.runner_id,
        )
        try:
            if req.op == "exec":
                self._run_exec(req, name, result)
            elif req.op == "put":
                self._run_put(req, result)
            elif req.op == "get":
                self._run_get(req, result)
            else:
                result.status = "rejected"
                result.error = f"未知 op：{req.op}"
        except Exception as exc:
            LOG.exception("执行 %s 失败", req.id)
            result.status = "failed"
            result.error = f"{type(exc).__name__}: {exc}"
        finally:
            result.finished_at = time.time()
            result.duration_ms = int((result.finished_at - started) * 1000)
            self._write_result(name, result)
            self._in_flight = max(0, self._in_flight - 1)
            self._safe_delete(self.store.outbox_progress(self.host_id, name))

    def _run_exec(self, req: Request, name: str, result: Result) -> None:
        """执行 shell 命令：独立进程组、超时杀进程组、大输出落 OSS。"""
        work_dir = os.path.join(self.cfg.work_dir, req.id)
        os.makedirs(work_dir, exist_ok=True)
        stdout_path = os.path.join(work_dir, "stdout.log")
        stderr_path = os.path.join(work_dir, "stderr.log")
        cwd = req.cwd or work_dir
        if not os.path.isdir(cwd):
            raise FileNotFoundError(f"工作目录不存在：{cwd}")
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (req.env or {}).items()})
        command = req.command()
        if not command.strip():
            raise ValueError("指令为空")
        LOG.info("执行 %s：%s", req.id, command)
        with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
            proc = subprocess.Popen(
                command,
                shell=True,
                executable="/bin/bash",
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,  # 独立进程组，超时才能整组杀掉
            )
            deadline = time.time() + float(req.timeout_s or 0)
            timed_out = False
            while True:
                try:
                    exit_code = proc.wait(timeout=5)
                    break
                except subprocess.TimeoutExpired:
                    self._write_progress(name, req, proc, stdout_path)
                    if time.time() > deadline:
                        timed_out = True
                        self._kill_process_group(proc)
                        exit_code = proc.wait(timeout=30)
                        break
        if timed_out:
            result.status = "timeout"
            result.error = f"执行超过 {req.timeout_s} 秒，已杀掉进程组"
        result.exit_code = exit_code
        max_bytes = int(req.max_output_bytes or self.cfg.max_output_bytes)
        result.stdout, result.stdout_truncated, result.stdout_object = self._read_output(
            stdout_path, self.store.file_key(req.id, "stdout.txt"), max_bytes
        )
        result.stderr, result.stderr_truncated, result.stderr_object = self._read_output(
            stderr_path, self.store.file_key(req.id, "stderr.txt"), max_bytes
        )
        if result.status != "timeout" and exit_code != 0:
            result.status = "failed"

    @staticmethod
    def _kill_process_group(proc: subprocess.Popen) -> None:
        """先 SIGTERM 再 SIGKILL，确保子进程树一起退出。"""
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pgid, sig)
            except OSError:
                return
            for _ in range(20):
                if proc.poll() is not None:
                    return
                time.sleep(0.25)

    def _write_progress(self, name: str, req: Request, proc: subprocess.Popen, stdout_path: str) -> None:
        """长任务定期写进度对象，本机侧可据此判断任务还活着。"""
        try:
            tail = ""
            if os.path.exists(stdout_path):
                size = os.path.getsize(stdout_path)
                with open(stdout_path, "rb") as handle:
                    handle.seek(max(0, size - 2048))
                    tail = handle.read().decode("utf-8", "replace")
            self.store.put_json(
                self.store.outbox_progress(self.host_id, name),
                {
                    "id": req.id,
                    "status": "running",
                    "elapsed_s": round(time.time() - req.created_at, 1),
                    "pid": proc.pid,
                    "stdout_tail": tail,
                    "updated_at": time.time(),
                },
            )
        except Exception as exc:
            LOG.debug("写进度失败：%s", exc)

    def _read_output(self, path: str, obj_key: str, max_bytes: int) -> tuple[str, bool, str]:
        """读取输出文件；超过阈值时把完整内容传 OSS，本地只保留首尾摘要。

        输出：(文本, 是否截断, 完整内容的 OSS key)。
        """
        if not os.path.exists(path):
            return "", False, ""
        size = os.path.getsize(path)
        if size <= max_bytes:
            with open(path, "rb") as handle:
                return handle.read().decode("utf-8", "replace"), False, ""
        self.store.upload_file(obj_key, path)
        with open(path, "rb") as handle:
            head = handle.read(max_bytes)
            handle.seek(max(0, size - 4096))
            tail = handle.read()
        text = head.decode("utf-8", "replace")
        text += f"\n...[输出被截断：共 {size} 字节，完整内容见 oss://{self.cfg.bucket}/{obj_key} ]...\n"
        text += tail.decode("utf-8", "replace")
        return text, True, obj_key

    def _run_put(self, req: Request, result: Result) -> None:
        """把 OSS 上的对象落到远端文件系统。"""
        for item in req.uploads:
            object_key = item.get("object")
            target = item.get("path")
            if not object_key or not target:
                raise ValueError(f"put 指令条目不完整：{item}")
            parent = os.path.dirname(os.path.abspath(target))
            os.makedirs(parent, exist_ok=True)
            self.store.download_file(object_key, target)
            result.artifacts.append(
                {"path": target, "object": object_key, "size": os.path.getsize(target)}
            )

    def _run_get(self, req: Request, result: Result) -> None:
        """把远端文件上传到 OSS。"""
        for item in req.downloads:
            source = item.get("path")
            object_key = item.get("object")
            if not source or not object_key:
                raise ValueError(f"get 指令条目不完整：{item}")
            if not os.path.isfile(source):
                raise FileNotFoundError(f"远端文件不存在：{source}")
            self.store.upload_file(object_key, source)
            result.artifacts.append(
                {"path": source, "object": object_key, "size": os.path.getsize(source)}
            )

    # ---------- 回写 ----------

    def _write_result(self, name: str, result: Result) -> None:
        """写结果对象并清理 pending/claimed。"""
        result_key = self.store.outbox_result(self.host_id, name)
        payload = result.to_payload()
        for attempt in range(3):
            try:
                self.store.put_json(result_key, payload)
                break
            except Exception as exc:
                LOG.warning("写结果 %s 失败（第 %s 次）：%s", name, attempt + 1, exc)
                time.sleep(0.5)
        else:
            LOG.error("写结果 %s 彻底失败，保留 claimed 等待重投", name)
            return
        try:
            self.store.put_json(
                self.store.inbox_done(self.host_id, name),
                {"id": result.id, "status": result.status, "finished_at": result.finished_at},
            )
        except Exception as exc:
            LOG.warning("写 done 标记失败：%s", exc)
        self._safe_delete(self.store.inbox_pending(self.host_id, name))
        self._safe_delete(self.store.inbox_claimed(self.host_id, name))
        work_dir = os.path.join(self.cfg.work_dir, result.id)
        shutil.rmtree(work_dir, ignore_errors=True)
        LOG.info("指令 %s 完成：status=%s exit_code=%s", name, result.status, result.exit_code)

    def _safe_delete(self, key: str) -> None:
        try:
            self.store.delete(key)
        except Exception as exc:
            LOG.debug("删除 %s 失败：%s", key, exc)

    # ---------- 主循环 ----------

    def run(self, duration_s: float | None = None) -> None:
        """启动 runner 主循环；duration_s 用于自测（到点自动退出）。"""
        self._started_at = time.time()
        self._in_flight = 0
        self.claim_identity()
        self.heartbeat_once()
        threads = [
            threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True),
        ]
        for thread in threads:
            thread.start()
        if not self.cfg.require_signature:
            LOG.warning(
                "已关闭指令签名校验（--insecure-no-signature）：任何能写 inbox 的人都能在本机执行命令"
            )
        LOG.info(
            "runner 启动：host_id=%s bucket=%s prefix=%s",
            self.host_id,
            self.cfg.bucket,
            self.cfg.prefix,
        )
        deadline = time.time() + duration_s if duration_s else None
        while not self._stop.is_set():
            if deadline and time.time() > deadline:
                LOG.info("达到运行时长上限，退出")
                break
            try:
                found = self.poll_once()
            except Exception as exc:
                LOG.warning("轮询异常：%s", exc)
                found = 0
            self._stop.wait(self.cfg.poll_interval_active if found else self.cfg.poll_interval_idle)
        self.shutdown()

    def shutdown(self) -> None:
        """优雅退出：停止轮询并等待在跑任务结束。"""
        self._stop.set()
        self.executor.shutdown(wait=True, cancel_futures=False)
        try:
            self.heartbeat_once()
        except Exception:
            pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="oss-bridge 远端 runner")
    parser.add_argument("--host-id", help="显式指定 host_id；不指定则自动推导并持久化")
    parser.add_argument("--instance", help="同一台机器跑多个 runner 时的实例名")
    parser.add_argument("--config", help="本项目 TOML 配置文件")
    parser.add_argument("--ossutil-config", help="ossutil 配置文件（读取 endpoint/AK/SK）")
    parser.add_argument("--sts-token-file", help="STS 凭证 JSON（如 /fuyao_oss_sts/token）")
    parser.add_argument("--endpoint")
    parser.add_argument("--bucket")
    parser.add_argument("--prefix")
    parser.add_argument("--ak")
    parser.add_argument("--sk")
    parser.add_argument("--secret", help="两端共享的 HMAC 密钥")
    parser.add_argument("--secret-file", help="从文件读取 HMAC 密钥（避免出现在 ps 里）")
    parser.add_argument(
        "--insecure-no-signature",
        action="store_true",
        help="关闭指令签名校验（仅适合私有目录：任何能写 inbox 的凭证都能在本机执行命令）",
    )
    parser.add_argument("--state-dir")
    parser.add_argument("--work-dir")
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--duration", type=float, help="仅运行 N 秒后退出（自测用）")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="打印最终生效的配置（含每项来源，密钥打码）后退出",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="打印配置并做一次 OSS 连通性检查后退出",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(clean_argv(argv))
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    cfg = load_config(
        config_file=args.config,
        ossutil_config=args.ossutil_config,
        sts_token_file=args.sts_token_file,
        secret_file=args.secret_file,
        overrides={
            "endpoint": args.endpoint,
            "bucket": args.bucket,
            "prefix": args.prefix,
            "access_key_id": args.ak,
            "access_key_secret": args.sk,
            "secret": args.secret,
            "require_signature": False if args.insecure_no_signature else None,
            "state_dir": args.state_dir,
            "work_dir": args.work_dir,
            "concurrency": args.concurrency,
        },
    )
    if args.print_config or args.check:
        print("最终生效配置：", file=sys.stderr)
        for line in describe(cfg):
            print("  " + line, file=sys.stderr)
        if args.check:
            try:
                store = OssStore(cfg)
                keys = store.list_keys(cfg.prefix, max_keys=10)
                print(f"  OSS 连通性  = OK（前缀下当前有 {len(keys)} 个对象）", file=sys.stderr)
            except Exception as exc:
                print(f"  OSS 连通性  = 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
                return 1
        return 0

    runner = Runner(cfg, host_id=args.host_id, instance=args.instance)

    def _handle_signal(signum, _frame):
        LOG.info("收到信号 %s，准备退出", signum)
        runner.shutdown()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    runner.run(duration_s=args.duration)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
