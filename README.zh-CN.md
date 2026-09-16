# oss-bridge

**让 Agent 拥有远程 shell —— 不需要 SSH、不开端口、不要公网 IP。**

oss-bridge 是一个 [MCP](https://modelcontextprotocol.io) 服务：把一个阿里云 OSS bucket
当作"指令队列 + 文件暂存区"。本机 Agent（Codex、Claude Code、Cursor 等）提交命令，
远端机器上的常驻小进程轮询 bucket、执行命令、把结果写回去。

```
┌─────────────┐    MCP     ┌───────────────────────────────┐    轮询     ┌──────────────┐
│  本机 Agent │◄──────────►│           OSS Bucket          │◄───────────►│  远程机器     │
│             │            │                               │             │              │
│ oss-bridge- │            │  registry/  机器注册与心跳      │             │ oss-bridge-  │
│ agent       │            │  hosts/<id>/inbox   指令队列    │             │ runner       │
│ (MCP server)│            │  hosts/<id>/outbox  结果队列    │             │ (常驻进程)    │
│             │            │  files/<job>/       大文件      │             │              │
└─────────────┘            └───────────────────────────────┘             └──────────────┘
```

English docs: [README.md](README.md).

## 为什么需要它

很多想让 Agent 操作的机器根本连不上：VPC 内的机器、NAT 后面的 GPU 服务器、不让开端口
也不让装 SSH 的专用设备。它们通常唯一有的，是能出网访问对象存储。

oss-bridge 就用这一条通道做所有事：

* **不需要入站访问**：远端只主动向 OSS 发出请求。
* **不需要额外基础设施**：不用消息队列、网关或 VPN。
* **文件也能传**：大命令输出和输入文件都走同一个 bucket，2GB 的包不会进模型的上下文。
* **支持多机**：每台 runner 自动注册，Agent 用 host_id、主机名或唯一前缀寻址。

代价是延迟：所有交互都是"存转"模式，一次往返约等于一个轮询间隔（默认 1~3 秒）。
它适合"提交任务、取结果"，不适合交互式或需要流式输出的场景。

## 环境要求

* Python **3.10+**（两端都是）
* 一个阿里云 OSS bucket，以及对该 bucket 某个前缀有读写权限的凭证
* `oss2`（作为依赖自动安装）

## 安装

```bash
pip install oss-bridge          # 发布到 PyPI 之后

# 或者从源码安装
git clone https://github.com/OWNER/oss-bridge && cd oss-bridge
pip install -e .
```

会提供两个命令：`oss-bridge-agent`（本机 MCP server）和 `oss-bridge-runner`（远端守护）。
直接跑源码也可以：`python3 -m ossbridge.mcp_server` / `python3 -m ossbridge.runner`。

## 快速开始

**1) 远程机器**：放好凭证和配置，启动 runner（一次性准备见下一节）：

```bash
python3 -m ossbridge.runner --ossutil-config ~/.oss-bridge/ossutilconfig \
    --endpoint oss-cn-hangzhou-internal.aliyuncs.com \
    --bucket your-bucket --prefix oss-bridge \
    --insecure-no-signature --state-dir ~/.oss-bridge/state
```

**2) 本机**：把 MCP server 注册给你的 Agent（见 [Codex 配置](#codex-配置)）。

**3) 让 Agent 调用** `remote_hosts`，然后 `remote_run`。

## 本机配置（MCP server）

`oss-bridge-agent` 走 stdio 传输，用命令行参数或环境变量配置：

| 参数 | 环境变量 | 含义 |
|---|---|---|
| `--endpoint` | `OSS_BRIDGE_ENDPOINT` | OSS endpoint，如 `oss-cn-hangzhou-internal.aliyuncs.com` |
| `--bucket` | `OSS_BRIDGE_BUCKET` | bucket 名 |
| `--prefix` | `OSS_BRIDGE_PREFIX` | bucket 内的工作前缀，默认 `oss-bridge/` |
| `--ossutil-config` | — | ossutil 配置文件（读取 endpoint/AK/SK） |
| `--sts-token-file` | — | STS 临时凭证 JSON |
| `--ak` / `--sk` | `OSS_BRIDGE_AK` / `OSS_BRIDGE_SK` | 直接给 AK/SK |
| `--secret-file` | — | 存放共享 HMAC 密钥的文件（开启签名校验） |
| `--insecure-no-signature` | — | 关闭签名校验（见[安全](#安全)） |
| `--config` | — | TOML 配置文件（模板见 `examples/bridge.toml`） |

优先级：命令行 > 环境变量 > 配置文件 > 内置默认值。

## 远程机器配置（runner）

runner 需要：能访问 OSS 的凭证与 endpoint、与本地一致的 bucket 和前缀、一个记录
`host_id` 的状态目录。

凭证来源按以下顺序尝试：`--ak/--sk` → `--ossutil-config` → `--sts-token-file`，
也都有对应的环境变量。推荐把常用配置写成一个文件：

```bash
mkdir -p ~/.oss-bridge
cp your.ossutilconfig ~/.oss-bridge/ossutilconfig
cat > ~/.oss-bridge/bridge.env <<'EOF'
OSS_BUCKET=your-bucket
OSS_PREFIX=oss-bridge
OSS_ENDPOINT=oss-cn-hangzhou-internal.aliyuncs.com
OSS_BRIDGE_NO_SIGNATURE=1
EOF
```

### host_id 会自动注册

首次启动时，runner 用主机名加上 machine-id/MAC 的哈希推导出一个稳定的 `host_id`
（例如 `gpu-node-01-3f9a2c`），持久化到 `--state-dir`，并每 15 秒写一次
`registry/<host_id>.json` 心跳。**这个 id 之后不会再变**——给运行中的机器改名会让
已排队但未执行的指令永久孤儿化。如果两台机器推导出同一个 id，后启动的那台会自动
退让为 `-2`、`-3`……

本机 Agent 会用注册表解析 `host` 参数，所以写完整 host_id、主机名或唯一前缀都可以。

### 作为服务运行

参考 [`examples/oss-bridge-runner.service`](examples/oss-bridge-runner.service)（systemd
单元，带基本加固）。请用专门的低权用户运行——runner 按设计会执行 shell 命令。

同一台机器跑多个 runner（不同项目/用户）时，给每个实例设置 `--instance <名字>` 和
独立的 `--state-dir`。

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

从源码运行时改成：

```toml
[mcp_servers.oss_bridge]
command = "python3"
args = ["-m", "ossbridge.mcp_server", "--endpoint", "…", "--bucket", "…", "--prefix", "…",
        "--insecure-no-signature"]
cwd = "/path/to/oss-bridge"
```

重启 Codex，然后让它列出远端机器——它应该调用 `remote_hosts` 并显示出你刚启动的 runner。

两点实践提醒：

* **工具超时**：`remote_run` 最多等 `wait_s` 秒（默认 20），超时会返回 `job_id` 而不是
  一直阻塞。告诉 Agent 可以用 `remote_status` 轮询。长时间的训练/编译任务务必走这条路。
* **签名开关两端必须一致**：一边验签、另一边不签名，请求会全部被拒；`--secret-file`
  内容不一致同理。

其它 MCP 客户端（Claude Code、Cursor、自研客户端）用法相同，把 stdio 命令配上即可。

## 工具列表

| 工具 | 参数 | 作用 |
|---|---|---|
| `remote_hosts` | — | 列出已注册的机器（在线状态、GPU、在跑任务数） |
| `remote_run` | `host`, `cmd`, `cwd?`, `timeout_s?`, `wait_s?` | 在远端执行 shell 命令 |
| `remote_status` | `host`, `job_id` | 查询任务状态，附带 runner 写的进度 |
| `remote_upload` | `host`, `local_path`, `remote_path`, `wait_s?` | 本机文件传到远端 |
| `remote_download` | `host`, `remote_path`, `local_path`, `wait_s?` | 远端文件取回本机 |
| `remote_output` | `host`, `job_id`, `stream?`, `max_bytes?` | 从 OSS 取回被截断的完整输出 |

结果包含 `status`（`ok` / `failed` / `timeout` / `rejected`）、`exit_code`、
`duration_ms`、`artifacts`。超过 `max_output_bytes` 的输出会传到 `files/<job>/`，
只在结果里带首尾摘要。

## OSS 目录结构

```
<prefix>/
  registry/<host_id>.json                     机器心跳（主机名、GPU、负载、版本）
  registry/<host_id>/owner.json               身份租约，处理 host_id 冲突
  hosts/<host_id>/inbox/pending/<job>.json    指令（本机写 → runner 读）
  hosts/<host_id>/inbox/claimed/<job>.json    原子抢占（forbid-overwrite）
  hosts/<host_id>/inbox/done/<job>.json       已处理标记
  hosts/<host_id>/outbox/result/<job>.json    结果（runner 写 → 本机读）
  hosts/<host_id>/outbox/progress/<job>.json  长任务进度
  files/<job>/...                             暂存输入与超长输出
  src/...                                     可选的自更新代码包
```

指令对象名以毫秒时间戳开头，runner 因此可以用 `start_after` 增量拉取，并周期性做一次
全量扫描兜底。

### 一条命令的完整流程

1. 本机（可选签名后）把请求 `PutObject` 到 `inbox/pending/`。
2. runner 列举该前缀、读取请求，然后用 `x-oss-forbid-overwrite: true` 创建
   `inbox/claimed/<job>`——这是一个条件 PUT，若已被别人抢占会返回 `409`。
   **前提是 bucket 未开启版本控制**；开启后该 header 失效，抢占语义不成立。
3. runner 在独立进程组里执行命令（超时会杀掉整个进程组），把结果写到
   `outbox/result/<job>`。
4. 写 `done` 标记，删除 pending/claimed 对象。

runner 中途崩溃时，抢占对象的租约（`timeout_s + lease_grace_s`）过期后会被重新投递
（`attempt + 1`）；超过 `max_attempts` 进死信（写一条 `failed` 结果）。结果对象一旦存在
就直接复用，同一个 job_id 不会被重复执行。

## 安全

**runner 就是一台远程 shell。** 请把该前缀当作特权资源：任何能往 `inbox/pending/`
写文件的人，都能让 runner 在远端执行命令。

签名校验**默认开启**。开启后每个请求都带 HMAC-SHA256 签名，密钥（`--secret-file`）
只存在两端本地配置里，**绝不写入 bucket**。这填补了"能写 bucket"和"能执行命令"之间的
鸿沟。

如果这个前缀是你个人的私有目录、只有你能写，可以用 `--insecure-no-signature`（**两端
都要加**）省掉这把密钥。你等于用 bucket 的 ACL 替代了一个独立密钥——当 ACL 真的只有你
时，这是个合理的取舍。

其它默认保护：

* 请求带时间戳，超出 ±15 分钟直接拒绝（防重放）。
* 指定给 `host_id` X 的请求，Y 机器会拒绝执行。
* 每个任务独立工作目录，超时杀整个进程组。
* [`examples/ram-policy.json`](examples/ram-policy.json) 给出了两端的最小权限策略示例。

## 运维

* **延迟**：一次往返约一个轮询间隔，空闲 2 秒、有任务 0.3 秒。
* **成本**：轮询是 `ListObjects` 请求，空闲退避已经压过一轮。
* **清理**：给 `files/`、`inbox/done/`、`outbox/result/` 配生命周期规则（比如 7 天过期），
  否则 bucket 会一直涨。
* **大文件**：单次 `PutObject` 上限 5GB，超过后 `oss2` 自动分片。只支持传单个文件，
  目录请先打包。

### 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 公网 endpoint 报 `No address associated with hostname` | bucket 级公网域名依赖通配 DNS，部分容器解析器会拒绝。改用 `-internal` 内网 endpoint。 |
| `InvalidArgument: max-keys must be an integer between 1 and 1000` | OSS 单次上限 1000；`store.list_keys` 已自动钳制。 |
| 抢占一直失败 | bucket 开了版本控制，`x-oss-forbid-overwrite` 失效。 |
| 请求全部 `rejected` | 两端签名开关不一致，或密钥不同。 |
| 任务被执行了两次 | 两个 runner 共用了同一个 `host_id`，给每个实例加 `--instance` 或用独立 state 目录。 |

## 可选：自更新部署

不想往每台远端机器拷代码，可以把代码发布到 bucket，再用一个引导脚本保持更新：

```bash
# 本机：打包并上传当前代码
python3 scripts/publish_src.py --ossutil-config ~/.oss-bridge/ossutilconfig \
    --endpoint oss-cn-hangzhou-internal.aliyuncs.com \
    --bucket your-bucket --prefix oss-bridge
```

会写入 `src/oss-bridge-src.tar.gz`、`src/latest.json`（sha256 + 文件清单）、
`src/releases/<时间>-<sha8>.tar.gz`（保留最近 5 个，可回滚）和 `src/remote_run.sh`。
打包是确定性的，代码没变会自动跳过上传。

远端机器只需自举一次，之后想更新就重跑同一个脚本：

```bash
ossutil64 cp oss://your-bucket/oss-bridge/src/remote_run.sh ./
bash remote_run.sh                       # 读取 ~/.oss-bridge/bridge.env
```

`remote_run.sh` 会比对 `latest.json` 与本地缓存，需要时下载校验新包、切换
`~/.oss-bridge/current`，然后启动 runner。重跑脚本就是全部的更新流程，配置示例见
[`examples/bridge.env.example`](examples/bridge.env.example)。

## 开发

```
ossbridge/
  config.py       配置合并（命令行 → 环境变量 → 配置文件 → 默认值）
  naming.py       host_id 推导、机器信息采集、心跳
  protocol.py     指令/结果信封与 HMAC 签名
  store.py        OSS 四个 API 的封装 + 原子抢占原语
  runner.py       远端守护进程
  client.py       本机侧客户端（MCP server 与测试共用）
  mcp_server.py   stdio MCP server（最小实现，无 SDK 依赖）
scripts/
  publish_src.py  打包并上传当前代码
  remote_run.sh   拉取最新代码并启动 runner
test/             针对真实 bucket 的端到端测试
```

测试会连接真实 OSS bucket 并在结束后自行清理；未配置测试凭证时会自动跳过：

```bash
export OSS_BRIDGE_TEST_ENDPOINT=oss-cn-hangzhou-internal.aliyuncs.com
export OSS_BRIDGE_TEST_BUCKET=your-test-bucket
export OSS_BRIDGE_TEST_PREFIX=oss-bridge-selftest/
export OSS_BRIDGE_TEST_OSSUTIL_CONFIG=~/.oss-bridge/ossutilconfig

python3 test/test_local_loop.py                  # 16 项：执行、异步、超时、文件、抢占
python3 test/test_mcp_stdio.py                   # 8 项：MCP 握手与工具调用
python3 test/test_remote_workflow.py             # 7 项：发布 → 远程自举 → 执行
python3 test/test_local_loop.py --no-signature   # 同上，关闭签名校验
```

设计说明写在各模块的 docstring 里，其中 `runner.py` 详细描述了抢占/租约/重试协议。

## 许可证

MIT，见 [LICENSE](LICENSE)。
