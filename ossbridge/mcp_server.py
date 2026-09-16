"""本机侧 MCP Server（stdio 传输）。

实现的是最小可用的 MCP 子集：initialize / tools/list / tools/call / ping。
刻意不依赖官方 mcp SDK，好处是这条链路上只有 oss2 一个第三方依赖，
在受限环境里部署阻力最小。协议消息一行一条 JSON，全部走 stdout；
日志一律走 stderr，否则会污染 MCP 通道。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Callable

from . import __version__
from .client import BridgeClient
from .config import ConfigError, clean_argv, describe, load_config

LOG = logging.getLogger("ossbridge.mcp")

DEFAULT_PROTOCOL_VERSION = "2024-11-05"


def _fmt_run(result: dict) -> str:
    """把执行结果整理成便于模型阅读的文本。"""
    lines = [
        f"status={result.get('status')} exit_code={result.get('exit_code')} "
        f"host_id={result.get('host_id')} job_id={result.get('job_id')} "
        f"duration_ms={result.get('duration_ms')}"
    ]
    if result.get("error"):
        lines.append(f"error={result['error']}")
    if result.get("hint"):
        lines.append(f"hint={result['hint']}")
    if result.get("progress"):
        lines.append(f"progress={json.dumps(result['progress'], ensure_ascii=False)}")
    stdout = result.get("stdout") or ""
    stderr = result.get("stderr") or ""
    if stdout:
        lines.append("--- stdout ---")
        lines.append(stdout.rstrip("\n"))
    if stderr:
        lines.append("--- stderr ---")
        lines.append(stderr.rstrip("\n"))
    if result.get("stdout_truncated") or result.get("stderr_truncated"):
        lines.append(
            "注意：输出被截断，可用 remote_output(host, job_id, stream) 读取更多内容"
        )
    if result.get("artifacts"):
        lines.append(f"artifacts={json.dumps(result['artifacts'], ensure_ascii=False)}")
    if result.get("local_path"):
        lines.append(f"local_path={result['local_path']} size={result.get('size')}")
    return "\n".join(lines)


def _fmt_hosts(hosts: list[dict]) -> str:
    """把机器列表整理成紧凑文本。"""
    if not hosts:
        return "注册表为空：还没有 runner 上报过心跳。"
    lines = [f"{len(hosts)} 台机器（online 表示心跳未过期）："]
    for item in hosts:
        state = "online" if item.get("online") else "offline"
        gpu = f" gpu={item.get('gpu_count')}x{item['gpu'][0]}" if item.get("gpu") else ""
        lines.append(
            f"- {item.get('host_id')}  [{state}] hostname={item.get('hostname')} "
            f"in_flight={item.get('in_flight')} last_heartbeat={item.get('last_heartbeat_s')}s"
            f"{gpu} cwd={item.get('cwd')}"
        )
    return "\n".join(lines)


class ToolBox:
    """工具集合：把 BridgeClient 的能力包装成 MCP 工具。"""

    def __init__(self, client: BridgeClient):
        self.client = client

    def specs(self) -> list[dict]:
        return [
            {
                "name": "remote_hosts",
                "description": "列出所有在 OSS 注册表里上报过心跳的远程机器（含在线状态、GPU、在跑任务数）。",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "remote_run",
                "description": (
                    "在指定远程机器上执行 shell 命令并返回结果。"
                    "host 可以写 host_id、hostname 或它们的唯一前缀；"
                    "超过 wait_s 未完成会返回 job_id，之后用 remote_status 查询。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string", "description": "目标机器（host_id/hostname/唯一前缀）"},
                        "cmd": {"type": "string", "description": "要执行的 shell 命令"},
                        "cwd": {"type": "string", "description": "远端工作目录，缺省为 runner 的临时目录"},
                        "timeout_s": {"type": "number", "description": "远端执行超时（秒），默认 600"},
                        "wait_s": {
                            "type": "number",
                            "description": "本次调用最多等待多少秒（默认 20），超时返回 job_id",
                        },
                    },
                    "required": ["host", "cmd"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "remote_status",
                "description": "查询某个 job_id 的状态；未完成时会附带 runner 写的进度信息。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string"},
                        "job_id": {"type": "string", "description": "remote_run 返回的 job_id"},
                    },
                    "required": ["host", "job_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "remote_upload",
                "description": "把本机文件上传到远程机器的指定路径（经 OSS 中转）。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string"},
                        "local_path": {"type": "string", "description": "本机文件路径"},
                        "remote_path": {"type": "string", "description": "远端目标路径"},
                        "wait_s": {"type": "number", "description": "最多等待秒数，默认 30"},
                    },
                    "required": ["host", "local_path", "remote_path"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "remote_download",
                "description": "把远程机器上的文件下载到本机路径（经 OSS 中转）。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string"},
                        "remote_path": {"type": "string", "description": "远端文件路径"},
                        "local_path": {"type": "string", "description": "本机保存路径"},
                        "wait_s": {"type": "number", "description": "最多等待秒数，默认 30"},
                    },
                    "required": ["host", "remote_path", "local_path"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "remote_output",
                "description": "读取某个任务被截断的完整输出（从 OSS 取回尾部内容）。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string"},
                        "job_id": {"type": "string"},
                        "stream": {"type": "string", "enum": ["stdout", "stderr"], "description": "默认 stdout"},
                        "max_bytes": {"type": "number", "description": "最多返回多少字节，默认 65536"},
                    },
                    "required": ["host", "job_id"],
                    "additionalProperties": False,
                },
            },
        ]

    def call(self, name: str, args: dict) -> str:
        """执行工具；返回值直接作为 MCP 文本内容。"""
        if name == "remote_hosts":
            return _fmt_hosts(self.client.hosts())
        if name == "remote_run":
            result = self.client.run(
                host=args["host"],
                cmd=args.get("cmd", ""),
                cwd=args.get("cwd", ""),
                timeout_s=float(args.get("timeout_s", 600)),
                wait_s=float(args.get("wait_s", 20)),
            )
            return _fmt_run(result)
        if name == "remote_status":
            return _fmt_run(self.client.status(args["host"], args["job_id"]))
        if name == "remote_upload":
            result = self.client.upload(
                host=args["host"],
                local_path=args["local_path"],
                remote_path=args["remote_path"],
                wait_s=float(args.get("wait_s", 30)),
            )
            return _fmt_run(result)
        if name == "remote_download":
            result = self.client.download(
                host=args["host"],
                remote_path=args["remote_path"],
                local_path=args["local_path"],
                wait_s=float(args.get("wait_s", 30)),
            )
            return _fmt_run(result)
        if name == "remote_output":
            payload = self.client.fetch_output(
                host=args["host"],
                job_id=args["job_id"],
                stream=args.get("stream", "stdout"),
                max_bytes=int(args.get("max_bytes", 65536)),
            )
            header = f"truncated={payload.get('truncated')} total_bytes={payload.get('total_bytes', '-')}"
            return header + "\n" + str(payload.get("text", ""))
        raise ValueError(f"未知工具：{name}")


class McpServer:
    """最小 MCP stdio 服务端。"""

    def __init__(self, toolbox: ToolBox):
        self.toolbox = toolbox

    def _handle(self, message: dict) -> dict | None:
        """处理一条 JSON-RPC 消息；通知类消息返回 None。"""
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params") or {}
        if msg_id is None:  # 通知，不需要回包
            return None
        if method == "initialize":
            requested = params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION
            return {
                "protocolVersion": requested,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "oss-bridge-agent", "version": __version__},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.toolbox.specs()}
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            try:
                text = self.toolbox.call(name, args)
                return {"content": [{"type": "text", "text": text}], "isError": False}
            except Exception as exc:
                LOG.exception("工具 %s 调用失败", name)
                return {
                    "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                    "isError": True,
                }
        if method in ("resources/list", "prompts/list"):
            return {"resources": []} if method == "resources/list" else {"prompts": []}
        return {"__error__": {"code": -32601, "message": f"不支持的方法：{method}"}}

    def serve_forever(self) -> int:
        """从 stdin 逐行读消息，把响应逐行写 stdout。"""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                LOG.warning("收到非法 JSON，已忽略")
                continue
            result = self._handle(message)
            if result is None:
                continue
            response: dict[str, Any] = {"jsonrpc": "2.0", "id": message.get("id")}
            if "__error__" in result:
                response["error"] = result["__error__"]
            else:
                response["result"] = result
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="oss-bridge 本机侧 MCP Server（stdio）")
    parser.add_argument("--config")
    parser.add_argument("--ossutil-config")
    parser.add_argument("--sts-token-file")
    parser.add_argument("--endpoint")
    parser.add_argument("--bucket")
    parser.add_argument("--prefix")
    parser.add_argument("--ak")
    parser.add_argument("--sk")
    parser.add_argument("--secret")
    parser.add_argument("--secret-file")
    parser.add_argument(
        "--insecure-no-signature",
        action="store_true",
        help="关闭指令签名校验（与 runner 保持一致才能互通）",
    )
    parser.add_argument("--state-dir")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="把最终生效的配置打印到 stderr 后退出（用于排查配置来源）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(clean_argv(argv))
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    try:
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
            },
        )
    except ConfigError as exc:
        LOG.error("配置错误：%s", exc)
        return 2
    if args.print_config:
        for line in describe(cfg):
            print(line, file=sys.stderr)
        return 0
    return McpServer(ToolBox(BridgeClient(cfg))).serve_forever()


if __name__ == "__main__":
    raise SystemExit(main())
