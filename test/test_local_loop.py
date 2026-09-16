"""本机端到端自测。

真的启动一个 runner 子进程（独立进程组），通过 OSS 收发指令与文件，
覆盖：执行成功/失败、异步任务、超时杀进程组、工作目录、大文件往返、
大输出落 OSS、验签拒绝、抢占互斥、host 别名解析。

用法：
    python3 test/test_local_loop.py

注意：用 ``--host-id selftest-<随机>`` 在 bucket 里建独立的 hosts/<id>/ 目录，
测试结束自动清理。每个用例打印 PASS/FAIL，最终返回非 0 表示有失败项。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _env_config import SKIP_MESSAGE, build_test_config, common_cli_args, test_env  # noqa: E402

from ossbridge.client import BridgeClient, HostNotResolved  # noqa: E402
from ossbridge.config import Config, load_config  # noqa: E402
from ossbridge.protocol import REQUEST_KIND, new_request_id, object_name  # noqa: E402
from ossbridge.store import ObjectExists, OssStore  # noqa: E402

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    """记录一条断言结果。"""
    RESULTS.append((bool(ok), label))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""), flush=True)


def build_config(env: dict, state_dir: str, tuning_file: str, secret: str, no_signature: bool) -> Config:
    """构造与 runner 子进程完全一致的配置。"""
    return build_test_config(
        env,
        secret=secret,
        state_dir=state_dir,
        tuning_file=tuning_file,
        no_signature=no_signature,
    )


def wait_for_registration(client: BridgeClient, host_id: str, timeout_s: float = 60.0) -> bool:
    """等待 runner 上报心跳。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if any(item.get("host_id") == host_id and item.get("online") for item in client.hosts()):
            return True
        time.sleep(1)
    return False


def cleanup_objects(store, host_id: str) -> int:
    """删除某台测试机器留下的全部对象，含 files/ 下它产生的文件。

    输入：store、host_id。输出：删除的对象数量。
    """
    cleaned = 0
    request_ids = set()
    for key in store.list_keys(store.hosts_prefix(host_id), max_keys=1000):
        if "/outbox/result/" in key:
            try:
                request_ids.add(str(store.get_json(key).get("id", "")))
            except Exception:
                pass
        store.delete(key)
        cleaned += 1
    for request_id in filter(None, request_ids):
        for key in store.list_keys(store.files_prefix(request_id), max_keys=200):
            store.delete(key)
            cleaned += 1
    for key in (store.registry_key(host_id), store.registry_owner_key(host_id)):
        if store.exists(key):
            store.delete(key)
            cleaned += 1
    return cleaned


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-signature", action="store_true", help="以关闭签名校验的方式自测")
    args = parser.parse_args()
    no_signature = args.no_signature
    env = test_env()
    if env is None:
        print(SKIP_MESSAGE)
        return 0
    run_id = uuid.uuid4().hex[:6]
    host_id = f"selftest-{run_id}"
    secret = "" if no_signature else secrets.token_hex(32)
    tmp_root = tempfile.mkdtemp(prefix="oss-bridge-selftest-")
    state_dir = os.path.join(tmp_root, "state")
    os.makedirs(state_dir, exist_ok=True)
    tuning_file = os.path.join(tmp_root, "bridge.toml")
    with open(tuning_file, "w", encoding="utf-8") as handle:
        # 自测时把轮询节奏调快，缩短用例等待时间
        handle.write(
            "[oss_bridge]\n"
            "poll_interval_idle = 0.5\n"
            "poll_interval_active = 0.2\n"
            "heartbeat_interval = 5.0\n"
            "full_sweep_interval = 10.0\n"
            "concurrency = 2\n"
        )

    cfg = build_config(env, state_dir, tuning_file, secret, no_signature)
    client = BridgeClient(cfg)
    store = OssStore(cfg)
    runner_log = os.path.join(tmp_root, "runner.log")
    runner_proc: subprocess.Popen | None = None
    try:
        log_handle = open(runner_log, "w", encoding="utf-8")
        runner_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "ossbridge.runner",
                "--config",
                tuning_file,
                *common_cli_args(env),
                *(("--insecure-no-signature",) if no_signature else ("--secret", secret)),
                "--state-dir",
                state_dir,
                "--host-id",
                host_id,
                "--log-level",
                "INFO",
            ],
            cwd=PROJECT_ROOT,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        check(wait_for_registration(client, host_id), "runner 注册心跳可见", f"host_id={host_id}")

        # 1) 基本执行
        result = client.run(host_id, cmd=f"echo hello-{run_id}", wait_s=30)
        check(
            result.get("status") == "ok" and f"hello-{run_id}" in (result.get("stdout") or ""),
            "执行成功并回传 stdout",
            f"status={result.get('status')} exit={result.get('exit_code')}",
        )

        # 2) 非零退出码
        result = client.run(host_id, cmd="echo boom >&2; exit 3", wait_s=30)
        check(
            result.get("status") == "failed" and result.get("exit_code") == 3,
            "非零退出码标记为 failed",
            f"status={result.get('status')} exit={result.get('exit_code')}",
        )

        # 3) 工作目录生效
        result = client.run(host_id, cmd="pwd", cwd="/tmp", wait_s=30)
        check(
            result.get("stdout", "").strip() == "/tmp",
            "cwd 参数生效",
            result.get("stdout", "").strip(),
        )

        # 4) 异步任务：wait_s 内完不成就返回 job_id
        started = time.time()
        result = client.run(host_id, cmd="sleep 6; echo done-async", wait_s=1)
        check(
            result.get("status") == "pending" and result.get("job_id"),
            "长任务先返回 job_id",
            f"status={result.get('status')} job_id={result.get('job_id')}",
        )
        job_id = result.get("job_id", "")
        final = None
        deadline = time.time() + 60
        while time.time() < deadline:
            status = client.status(host_id, job_id)
            if status.get("status") not in ("pending", "running", "unknown"):
                final = status
                break
            time.sleep(2)
        check(
            final is not None
            and final.get("status") == "ok"
            and "done-async" in (final.get("stdout") or ""),
            "异步任务最终完成",
            f"status={(final or {}).get('status')} 耗时={time.time() - started:.1f}s",
        )

        # 5) 超时杀进程组
        started = time.time()
        result = client.run(host_id, cmd="sleep 60 & sleep 60", timeout_s=3, wait_s=60)
        elapsed = time.time() - started
        check(
            result.get("status") == "timeout" and elapsed < 40,
            "超时任务被杀掉进程组",
            f"status={result.get('status')} 耗时={elapsed:.1f}s error={result.get('error')}",
        )

        # 6) 大输出落 OSS + 尾部可读
        result = client.run(host_id, cmd="seq 1 50000", max_output_bytes=4096, wait_s=60)
        check(
            result.get("status") == "ok"
            and result.get("stdout_truncated")
            and result.get("stdout_object"),
            "大输出被截断并把完整内容写 OSS",
            f"truncated={result.get('stdout_truncated')} object={bool(result.get('stdout_object'))}",
        )
        tail = client.fetch_output(host_id, result.get("job_id", ""), "stdout", max_bytes=4096)
        check("50000" in (tail.get("text") or ""), "可从 OSS 取回被截断输出的尾部")

        # 7) 文件往返：本机 → 远端 → 本机
        payload = os.urandom(256 * 1024)
        local_src = os.path.join(tmp_root, "upload.bin")
        with open(local_src, "wb") as handle:
            handle.write(payload)
        remote_path = f"/tmp/oss-bridge-{run_id}.bin"
        up = client.upload(host_id, local_src, remote_path, wait_s=60)
        check(up.get("status") == "ok", "文件上传到远端", f"status={up.get('status')}")
        hash_check = client.run(host_id, cmd=f"sha256sum {remote_path} | cut -d' ' -f1", wait_s=60)
        expected = hashlib.sha256(payload).hexdigest()
        check(
            hash_check.get("stdout", "").strip() == expected,
            "远端文件内容与本机一致",
            f"remote={hash_check.get('stdout', '').strip()[:16]} local={expected[:16]}",
        )
        local_dst = os.path.join(tmp_root, "download.bin")
        down = client.download(host_id, remote_path, local_dst, wait_s=60)
        ok_down = down.get("status") == "ok" and os.path.isfile(local_dst)
        same = ok_down and hashlib.sha256(open(local_dst, "rb").read()).hexdigest() == expected
        check(bool(same), "文件从远端下载回本机且内容一致")
        client.run(host_id, cmd=f"rm -f {remote_path}", wait_s=30)

        # 8) 抢占互斥：同一个 claimed 对象只能创建一次
        claim_key = store.inbox_claimed(host_id, "claimtest.json")
        store.put_json(claim_key, {"test": True}, forbid_overwrite=True)
        conflict = False
        try:
            store.put_json(claim_key, {"test": True}, forbid_overwrite=True)
        except ObjectExists:
            conflict = True
        store.delete(claim_key)
        check(conflict, "forbid-overwrite 构成原子抢占（第二次写入被拒）")

        # 9) 签名模式：伪造签名必须被拒绝；无签名模式：不带签名的指令应当执行
        if no_signature:
            unsigned_id = new_request_id()
            unsigned_name = object_name(unsigned_id)
            store.put_json(
                store.inbox_pending(host_id, unsigned_name),
                {
                    "kind": REQUEST_KIND,
                    "payload": {
                        "id": unsigned_id,
                        "host_id": host_id,
                        "op": "exec",
                        "created_at": time.time(),
                        "cmd": f"echo unsigned-{run_id}",
                    },
                },
            )
            unsigned_result = client.wait(host_id, unsigned_name, 30)
            check(
                unsigned_result is not None
                and unsigned_result.get("status") == "ok"
                and f"unsigned-{run_id}" in (unsigned_result.get("stdout") or ""),
                "无签名指令被正常执行（已关闭校验）",
                f"status={(unsigned_result or {}).get('status')}",
            )
            hostname = next(
                (item["hostname"] for item in client.hosts() if item.get("host_id") == host_id),
                None,
            )
            check(
                client.resolve_host(host_id[:12]) == host_id,
                "host 前缀解析正常",
                f"hostname={hostname}",
            )
            failed = [label for ok, label in RESULTS if not ok]
            print(f"\n共 {len(RESULTS)} 项，失败 {len(failed)} 项")
            if failed:
                print("失败项：" + "；".join(failed))
                return 1
            return 0

        bad_id = new_request_id()
        bad_name = object_name(bad_id)
        store.put_json(
            store.inbox_pending(host_id, bad_name),
            {
                "kind": REQUEST_KIND,
                "payload": {
                    "id": bad_id,
                    "host_id": host_id,
                    "op": "exec",
                    "created_at": time.time(),
                    "cmd": f"touch /tmp/oss-bridge-should-not-exist-{run_id}",
                },
                "sig": "deadbeef",
            },
        )
        bad_result = client.wait(host_id, bad_name, 30)
        check(
            bad_result is not None and bad_result.get("status") == "rejected",
            "伪造签名的指令被拒绝",
            f"status={(bad_result or {}).get('status')}",
        )
        check(
            not os.path.exists(f"/tmp/oss-bridge-should-not-exist-{run_id}"),
            "被拒绝的指令没有产生副作用",
        )

        # 10) host 别名解析
        hostname = next(
            (item["hostname"] for item in client.hosts() if item.get("host_id") == host_id), None
        )
        resolved_ok = True
        detail = f"hostname={hostname}"
        try:
            resolved_ok = client.resolve_host(host_id[:12]) == host_id
            if hostname:
                try:
                    resolved_ok = resolved_ok and client.resolve_host(hostname) == host_id
                except HostNotResolved as exc:
                    # 同一台机器上跑多个实例时 hostname 必然歧义，只要候选里含本实例即算通过
                    resolved_ok = resolved_ok and host_id in str(exc)
                    detail += "（hostname 有歧义，已按候选列表处理）"
        except Exception as exc:
            resolved_ok = False
            detail += f" 异常={exc}"
        check(resolved_ok, "hostname / 前缀都能解析到 host_id", detail)

    finally:
        if runner_proc and runner_proc.poll() is None:
            try:
                os.killpg(os.getpgid(runner_proc.pid), signal.SIGTERM)
            except OSError:
                pass
            try:
                runner_proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(runner_proc.pid), signal.SIGKILL)
        try:
            cleaned = cleanup_objects(store, host_id)
        except Exception as exc:
            cleaned = -1
            print("清理 OSS 失败：", exc)
        print(f"已清理 {cleaned} 个测试对象（host_id={host_id}）")
        print(f"runner 日志：{runner_log}")
        if os.environ.get("KEEP_SELFTEST_DIR") != "1":
            shutil.rmtree(tmp_root, ignore_errors=True)

    failed = [label for ok, label in RESULTS if not ok]
    print(f"\n共 {len(RESULTS)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：" + "；".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
