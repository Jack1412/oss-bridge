# oss-bridge

**让 Agent 拥有远程 shell —— 不需要 SSH、不开端口、不要公网 IP。**

oss-bridge 是一个 [MCP](https://modelcontextprotocol.io) 服务：把一个阿里云 OSS bucket
当作"指令队列 + 文件暂存区"。本机 Agent 提交命令，远端机器上的常驻进程轮询 bucket、
执行命令、把结果写回去。

```
┌─────────────┐    MCP     ┌──────────────┐    轮询     ┌──────────────┐
│  本机 Agent │◄──────────►│  OSS Bucket  │◄───────────►│  远程机器     │
│ oss-bridge- │            │  inbox/      │             │ oss-bridge-  │
│ agent       │            │  outbox/     │             │ runner       │
│             │            │  files/      │             │              │
└─────────────┘            └──────────────┘             └──────────────┘
```

[English docs](README.md) · [原理与运维细节](docs/internals.md)

## 为什么

想让 Agent 操作的机器常常连不上——VPC 内的机器、NAT 后面的 GPU 服务器、不让开端口的
设备。它们通常唯一有的，是能出网访问对象存储：

- **不需要入站访问** —— 远端只向 OSS 主动发请求。
- **不需要额外基础设施** —— 不用消息队列、网关或 VPN。
- **文件也能传** —— 输入文件和超长输出都走同一个 bucket，2GB 的包不会进模型上下文。

代价是延迟：所有交互都是"存转"模式，一次往返约一个轮询间隔（默认 1~3 秒）。
它适合"提交任务、取结果"，不适合交互式或需要流式输出的场景。

## 安装

```bash
pip install oss-bridge        # 或 pipx install oss-bridge

# 或者从源码安装
git clone https://github.com/Jack1412/oss-bridge && cd oss-bridge
pip install -e .
```

会提供两个命令：`oss-bridge-agent`（本机 MCP server）与 `oss-bridge-runner`（远端守护）。
也可以直接 `python3 -m ossbridge.mcp_server` / `-m ossbridge.runner`。

> **远端机器可以不安装。** runner 直接在代码目录里跑，
> [`scripts/remote_run.sh`](scripts/remote_run.sh) 会把唯一依赖装进本地 `vendor/` 目录，
> 不碰系统 Python、不需要 sudo。
>
> Debian/Ubuntu 上 pip 可能报 `error: externally-managed-environment`（PEP 668）。
> 用虚拟环境，或者装到家目录：`pip install --user --break-system-packages -e .`。
> 该参数只影响安装位置（`~/.local`），不会动系统包。

## 快速开始

**远程机器**：启动守护进程（无需安装，它会自动注册并开始轮询）：

```bash
python3 -m ossbridge.runner \
    --ossutil-config ~/.oss-bridge/ossutilconfig \
    --endpoint oss-cn-hangzhou-internal.aliyuncs.com \
    --bucket your-bucket --prefix oss-bridge \
    --insecure-no-signature --state-dir ~/.oss-bridge/state
```

长期运行建议用 [systemd 单元](examples/oss-bridge-runner.service)。

**本机**：把 MCP server 注册给你的 Agent（见下一节），然后让它调用 `remote_hosts`
和 `remote_run`。

## Codex 配置

在 `~/.codex/config.toml` 里加：

```toml
[mcp_servers.oss_bridge]
command = "oss-bridge-agent"
args = [
  "--endpoint", "oss-cn-hangzhou-internal.aliyuncs.com",
  "--bucket", "your-bucket",
  "--prefix", "oss-bridge",
  "--ossutil-config", "/home/USER/.oss-bridge/ossutilconfig",
  "--insecure-no-signature",
]
```

从源码运行时改成 `command = "python3"`、
`args = ["-m", "ossbridge.mcp_server", ...]`，并加上 `cwd = "/path/to/oss-bridge"`。

重启 Codex，然后让它列出远端机器即可。其它 MCP 客户端（Claude Code、Cursor 等）用法
相同，配上同一个 stdio 命令就行。

两点要注意：

- `remote_run` 最多等 `wait_s` 秒（默认 20），超时返回 `job_id`，之后用
  `remote_status` 轮询。长时间任务务必走这条路。
- 签名开关两端必须一致，否则请求会全部被拒。

## 工具

| 工具 | 参数 | 作用 |
|---|---|---|
| `remote_hosts` | — | 列出已注册机器（在线状态、GPU、在跑任务数） |
| `remote_run` | `host`, `cmd`, `cwd?`, `timeout_s?`, `wait_s?` | 在远端执行 shell 命令 |
| `remote_status` | `host`, `job_id` | 查询任务状态，含长任务进度 |
| `remote_upload` | `host`, `local_path`, `remote_path` | 本机文件传到远端 |
| `remote_download` | `host`, `remote_path`, `local_path` | 远端文件取回本机 |
| `remote_output` | `host`, `job_id`, `stream?` | 从 OSS 取回被截断的完整输出 |

`host` 可以写完整 host_id、主机名，或它们各自的唯一前缀。

## 配置

两端都用 `--endpoint`、`--bucket`、`--prefix`（环境变量 `OSS_BRIDGE_ENDPOINT` /
`OSS_BRIDGE_BUCKET` / `OSS_BRIDGE_PREFIX`），凭证来自 `--ossutil-config`、
`--sts-token-file` 或 `--ak/--sk`。优先级：命令行 → 环境变量 → TOML 文件，
所以 `--config examples/bridge.toml` 两端通用。

模板：[bridge.env](examples/bridge.env.example) ·
[bridge.toml](examples/bridge.toml) · [ram-policy.json](examples/ram-policy.json)

## 安全

runner 就是一台远程 shell：任何能往 `inbox/` 写文件的人，都能让它在远端执行命令。
签名校验**默认开启**，每个请求带 HMAC-SHA256 签名，密钥只存在两端本地、绝不写入
bucket，补齐了"能写 bucket"和"能执行命令"之间的鸿沟。

如果这个前缀是你个人的私有目录、只有你能写，可以两端都加 `--insecure-no-signature`，
省掉这把密钥。另外请求带时间戳（超过 15 分钟视为重放直接拒绝），且指定给某台机器的
请求不会被别的机器执行。

## 运维

- 给 `files/`、`inbox/done/`、`outbox/result/` 配生命周期规则，否则 bucket 会一直涨。
- **用 `-internal` 内网 endpoint**：bucket 级公网域名依赖通配 DNS，部分容器解析不了。
- **保持 bucket 未开启版本控制**，否则 `forbid-overwrite` 抢占原语失效，同一任务可能被
  两台 runner 重复执行。
- 只支持传单个文件，目录请先打包。

OSS 目录结构、抢占/租约协议与故障排查见 [docs/internals.md](docs/internals.md)。

## 自更新部署

不想往每台机器拷代码，可以把代码发布到 bucket，远端只跑一个引导脚本，重跑即更新：

```bash
python3 scripts/publish_src.py --ossutil-config ~/.oss-bridge/ossutilconfig \
    --endpoint oss-cn-hangzhou-internal.aliyuncs.com \
    --bucket your-bucket --prefix oss-bridge
```

远端的具体做法见 [docs/internals.md](docs/internals.md)。

## 开发

测试会连接真实 bucket 并自行清理；未配置凭证时自动跳过。设置
`OSS_BRIDGE_TEST_ENDPOINT`、`OSS_BRIDGE_TEST_BUCKET`、`OSS_BRIDGE_TEST_PREFIX`、
`OSS_BRIDGE_TEST_OSSUTIL_CONFIG` 后：

```bash
python3 test/test_local_loop.py        # 执行/异步/超时/文件/抢占（16 项）
python3 test/test_mcp_stdio.py         # MCP 握手与工具调用（8 项）
python3 test/test_remote_workflow.py   # 发布 → 远程自举 → 执行（7 项）
```

## 许可证

MIT，见 [LICENSE](LICENSE)。
