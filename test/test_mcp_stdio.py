"""MCP stdio 层自测。

按真实链路来：启动 runner → 把 oss-bridge-agent 当子进程拉起 →
用 JSON-RPC 逐行发 initialize / tools/list / tools/call → 校验响应。
这一层过了，Codex 侧配置好 command 就能直接用。

用法：
    python3 test/test_mcp_stdio.py
"""

from __future__ import annotations

import argparse
import json
import os
import select
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
    test_env,
)

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    """记录一条断言结果。"""
    RESULTS.append((bool(ok), label))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""), flush=True)


class McpClient:
    """极简 MCP stdio 客户端。"""

    def __init__(self, proc: subprocess.Popen, timeout_s: float = 60.0):
        self.proc = proc
        self.timeout_s = timeout_s
        self._next_id = 1

    def _read_line(self, deadline: float) -> str:
        while time.time() < deadline:
            ready, _, _ = select.select([self.proc.stdout], [], [], 1.0)
            if not ready:
                continue
            line = self.proc.stdout.readline()
            if not line:
                return ""
            line = line.strip()
            if line:
                return line
        return ""

    def request(self, method: str, params: dict | None = None) -> dict:
        """发一条请求并等待对应 id 的响应。"""
        msg_id = self._next_id
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            payload["params"] = params
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + self.timeout_s
        while True:
            line = self._read_line(deadline)
            if not line:
                raise TimeoutError(f"{method} 未在 {self.timeout_s}s 内返回")
            message = json.loads(line)
            if message.get("id") == msg_id:
                return message

    def notify(self, method: str, params: dict | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()


def call_tool(client: McpClient, name: str, args: dict | None = None) -> tuple[bool, str]:
    """调用工具，返回 (isError, 文本)。"""
    message = client.request("tools/call", {"name": name, "arguments": args or {}})
    result = message.get("result") or {}
    content = result.get("content") or []
    text = content[0].get("text", "") if content else ""
    return bool(result.get("isError")), text


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
    host_id = f"mcp-selftest-{run_id}"
    secret = "" if no_signature else secrets.token_hex(32)
    tmp_root = tempfile.mkdtemp(prefix="oss-bridge-mcp-")
    state_dir = os.path.join(tmp_root, "state")
    os.makedirs(state_dir, exist_ok=True)
    tuning_file = os.path.join(tmp_root, "bridge.toml")
    with open(tuning_file, "w", encoding="utf-8") as handle:
        handle.write(
            "[oss_bridge]\n"
            "poll_interval_idle = 0.5\n"
            "poll_interval_active = 0.2\n"
            "heartbeat_interval = 5.0\n"
            "full_sweep_interval = 10.0\n"
        )
    common_args = [
        "--config",
        tuning_file,
        *common_cli_args(env),
        *(("--insecure-no-signature",) if no_signature else ("--secret", secret)),
        "--state-dir",
        state_dir,
    ]
    runner_proc: subprocess.Popen | None = None
    mcp_proc: subprocess.Popen | None = None
    try:
        runner_log = open(os.path.join(tmp_root, "runner.log"), "w", encoding="utf-8")
        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "ossbridge.runner", *common_args, "--host-id", host_id],
            cwd=PROJECT_ROOT,
            stdout=runner_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        time.sleep(8)  # 等 runner 完成注册并写出心跳

        mcp_err = open(os.path.join(tmp_root, "mcp.err"), "w", encoding="utf-8")
        mcp_proc = subprocess.Popen(
            [sys.executable, "-m", "ossbridge.mcp_server", *common_args],
            cwd=PROJECT_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=mcp_err,
            text=True,
            start_new_session=True,
            bufsize=1,
        )
        client = McpClient(mcp_proc)

        init = client.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "selftest", "version": "1"},
            },
        )
        result = init.get("result") or {}
        check(
            result.get("protocolVersion") == "2024-11-05"
            and (result.get("serverInfo") or {}).get("name") == "oss-bridge-agent",
            "initialize 握手成功",
            json.dumps(result.get("serverInfo"), ensure_ascii=False),
        )
        client.notify("notifications/initialized")

        tools = client.request("tools/list").get("result", {}).get("tools", [])
        names = [tool["name"] for tool in tools]
        check(
            all(
                name in names
                for name in (
                    "remote_hosts",
                    "remote_run",
                    "remote_status",
                    "remote_upload",
                    "remote_download",
                    "remote_output",
                )
            ),
            "tools/list 返回全部工具",
            ",".join(names),
        )

        is_error, text = call_tool(client, "remote_hosts")
        check(not is_error and host_id in text, "remote_hosts 能看到 runner", text.splitlines()[-1][:90])

        is_error, text = call_tool(
            client, "remote_run", {"host": host_id, "cmd": f"echo mcp-{run_id}", "wait_s": 30}
        )
        check(
            not is_error and f"mcp-{run_id}" in text and "status=ok" in text,
            "remote_run 通过 MCP 执行成功",
            text.replace("\n", " | ")[:110],
        )

        is_error, text = call_tool(client, "remote_run", {"host": host_id, "cmd": "exit 7", "wait_s": 30})
        check(not is_error and "exit_code=7" in text, "非零退出码经 MCP 正确回传", text.splitlines()[0])

        is_error, text = call_tool(
            client, "remote_run", {"host": host_id, "cmd": "sleep 4; echo x", "wait_s": 1}
        )
        check(not is_error and "status=pending" in text, "异步任务经 MCP 返回 job_id", text.splitlines()[0])

        is_error, text = call_tool(client, "nonexistent_tool", {})
        check(is_error and "未知工具" in text, "调用不存在的工具返回 isError")

        is_error, text = call_tool(client, "remote_run", {"host": "no-such-host", "cmd": "echo x"})
        check(is_error and "找不到匹配" in text, "host 解析失败时返回可读错误", text[:80])

    finally:
        for proc in (mcp_proc, runner_proc):
            if proc and proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except OSError:
                    proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except OSError:
                        proc.kill()
        # 清理测试对象
        from ossbridge.config import load_config
        from ossbridge.store import OssStore

        cfg = build_test_config(
            env,
            secret=secret,
            state_dir=state_dir,
            tuning_file=tuning_file,
            no_signature=no_signature,
        )
        store = OssStore(cfg)
        try:
            cleaned = cleanup_objects(store, host_id)
        except Exception as exc:
            cleaned = -1
            print("清理 OSS 失败：", exc)
        print(f"已清理 {cleaned} 个测试对象（host_id={host_id}）")
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
