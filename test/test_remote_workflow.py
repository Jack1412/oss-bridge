"""远程工作流自测：发布到 OSS → 远程脚本拉取代码并启动 → 通过客户端执行命令。

模拟的就是真实场景：本机发布代码，远程机器只跑 ``scripts/remote_run.sh``。

用法：
    python3 test/test_remote_workflow.py

注意：会先执行一次发布（内容没变时会跳过），并在 bucket 里建一个
``remote-sim-*`` 的独立 host 目录，测试后清理自己的对象；已发布的 src/ 保留。
"""

from __future__ import annotations

import argparse
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

from _env_config import (  # noqa: E402
    SKIP_MESSAGE,
    build_test_config,
    common_cli_args,
    remote_script_env,
    test_env,
)

from ossbridge.client import BridgeClient  # noqa: E402
from ossbridge.config import load_config  # noqa: E402
from ossbridge.store import OssStore  # noqa: E402

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    """记录一条断言结果。"""
    RESULTS.append((bool(ok), label))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""), flush=True)


def cleanup_objects(store: OssStore, host_id: str) -> int:
    """删除该测试 host 留下的全部对象（含 files/ 下它产生的文件）。"""
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
    host_id = f"remote-sim-{run_id}"
    secret = "" if no_signature else secrets.token_hex(32)
    tmp_root = tempfile.mkdtemp(prefix="oss-bridge-remote-")
    sim_root = os.path.join(tmp_root, "remote-home")
    os.makedirs(sim_root, exist_ok=True)
    secret_file = os.path.join(sim_root, "secret")
    if not no_signature:
        with open(secret_file, "w", encoding="utf-8") as handle:
            handle.write(secret + "\n")
        os.chmod(secret_file, 0o600)
    sim_log = os.path.join(tmp_root, "remote_run.log")
    run_proc: subprocess.Popen | None = None

    cfg = build_test_config(
        env,
        secret=secret,
        state_dir=os.path.join(sim_root, "state"),
        no_signature=no_signature,
    )
    client = BridgeClient(cfg)
    store = OssStore(cfg)
    try:
        # 1) 发布代码到 OSS src/
        publish_args = [sys.executable, "scripts/publish_src.py", *common_cli_args(env)]
        publish = subprocess.run(
            publish_args,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        check(publish.returncode == 0, "发布脚本执行成功", (publish.stdout or publish.stderr).strip()[:90])
        metadata = store.get_json(cfg.prefix + "src/latest.json")
        check(bool(metadata.get("sha256")), "latest.json 含 sha256", metadata.get("sha256", "")[:16])

        # 2) 模拟远程机器：只跑 remote_run.sh（凭证候选里 fuyao STS 无权限，会回退到 ossutil 配置）
        child_env = dict(os.environ)
        child_env.update(remote_script_env(env, sim_root, secret_file, no_signature))
        log_handle = open(sim_log, "w", encoding="utf-8")
        run_proc = subprocess.Popen(
            [
                "bash",
                "scripts/remote_run.sh",
                "--host-id",
                host_id,
                "--duration",
                "60",
                "--log-level",
                "INFO",
            ],
            cwd=PROJECT_ROOT,
            env=child_env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

        # 3) 等待远程 runner 注册
        registered = False
        deadline = time.time() + 90
        while time.time() < deadline:
            if any(item.get("host_id") == host_id and item.get("online") for item in client.hosts()):
                registered = True
                break
            if run_proc.poll() is not None:
                break
            time.sleep(1)
        check(registered, "远程脚本拉取代码并注册成功", f"host_id={host_id}")

        # 4) 通过客户端在"远程"执行命令
        result = client.run(host_id, cmd=f"echo workflow-{run_id}", wait_s=60)
        check(
            result.get("status") == "ok" and f"workflow-{run_id}" in (result.get("stdout") or ""),
            "通过远程脚本拉起的 runner 能执行命令",
            f"status={result.get('status')} stdout={(result.get('stdout') or '').strip()[:40]}",
        )

        # 5) 校验远程侧确实做了代码拉取与解压
        with open(sim_log, encoding="utf-8") as handle:
            log_text = handle.read()
        releases = os.path.join(sim_root, "releases")
        extracted = sorted(os.listdir(releases)) if os.path.isdir(releases) else []
        check(
            "拉取代码" in log_text and bool(extracted),
            "本地缓存里出现了拉取到的代码版本",
            f"releases={extracted}",
        )
        short = metadata["sha256"][:8]
        check(short in extracted, "解压目录名与发布 sha256 前 8 位一致", f"{short} in {extracted}")

        # 6) 重复启动应命中缓存、不再重复解压
        started = time.time()
        rerun = subprocess.run(
            ["bash", "scripts/remote_run.sh", "--host-id", host_id, "--duration", "3"],
            cwd=PROJECT_ROOT,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        check(
            rerun.returncode == 0 and f"代码目录：{releases}/{short}" in (rerun.stderr + rerun.stdout),
            "再次启动复用同一版本代码",
            f"{time.time() - started:.1f}s",
        )
    finally:
        if run_proc and run_proc.poll() is None:
            try:
                os.killpg(os.getpgid(run_proc.pid), signal.SIGTERM)
            except OSError:
                pass
            try:
                run_proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(run_proc.pid), signal.SIGKILL)
        try:
            cleaned = cleanup_objects(store, host_id)
        except Exception as exc:
            cleaned = -1
            print("清理 OSS 失败：", exc)
        print(f"已清理 {cleaned} 个测试对象（host_id={host_id}）")
        print(f"远程脚本日志：{sim_log}")
        if os.environ.get("KEEP_SELFTEST_DIR") != "1":
            shutil.rmtree(tmp_root, ignore_errors=True)

    failed = [label for ok, label in RESULTS if not ok]
    if failed:
        try:
            with open(sim_log, encoding="utf-8") as handle:
                tail = handle.read().splitlines()[-15:]
            print("--- 远程脚本日志尾部 ---")
            print("\n".join(tail))
        except OSError:
            pass
    print(f"\n共 {len(RESULTS)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：" + "；".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
