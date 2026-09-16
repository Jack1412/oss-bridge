"""oss-bridge：把 OSS 当作双向消息队列 + 文件暂存区。

本机侧（agent）与远程侧（runner）都只与 OSS 打交道，互不直接通信：
    本机 Agent <--MCP--> oss-bridge-agent <--OSS--> oss-bridge-runner --执行--> 远程机器
"""

__version__ = "0.1.0"
