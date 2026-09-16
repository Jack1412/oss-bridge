# oss-bridge

**Give your coding agent a remote shell — without SSH, open ports, or a public IP.**

oss-bridge is an [MCP](https://modelcontextprotocol.io) server that turns an Alibaba Cloud
OSS bucket into a job queue plus file staging area. Your local agent submits a command; a
small daemon on the remote host polls the bucket, runs it, and writes the result back.

```
┌─────────────┐    MCP     ┌──────────────┐   polling   ┌──────────────┐
│  Local      │◄──────────►│  OSS bucket  │◄───────────►│ Remote host  │
│  agent      │            │  inbox/      │             │ oss-bridge-  │
│ oss-bridge- │            │  outbox/     │             │ runner       │
│ agent       │            │  files/      │             │              │
└─────────────┘            └──────────────┘             └──────────────┘
```

[中文文档](README.zh-CN.md) · [Internals & operations](docs/internals.md)

## Why

Machines you want an agent to drive are often unreachable — private VPC boxes, GPU workers
behind NAT, appliances where you cannot open a port. What they usually *can* do is reach
object storage over outbound HTTPS:

- **No inbound access** — the remote host only makes outbound requests.
- **No extra infrastructure** — no broker, no gateway, no VPN.
- **Files included** — input files and oversized output travel through the bucket, so a
  2 GB tarball never enters the model's context window.

The trade-off: everything is store-and-forward, so a round trip takes about one poll
interval (1–3 s by default). It is built for *submit a job, fetch the result*, not for
interactive or streaming sessions.

## Install

```bash
pip install oss-bridge        # once published to PyPI

# or from source
git clone https://github.com/Jack1412/oss-bridge && cd oss-bridge
pip install -e .
```

Gives you two commands: `oss-bridge-agent` (local MCP server) and `oss-bridge-runner`
(remote daemon). `python3 -m ossbridge.mcp_server` / `-m ossbridge.runner` work too.

## Quick start

**Remote host** — start the daemon (it registers itself and begins polling):

```bash
python3 -m ossbridge.runner \
    --ossutil-config ~/.oss-bridge/ossutilconfig \
    --endpoint oss-cn-hangzhou-internal.aliyuncs.com \
    --bucket your-bucket --prefix oss-bridge \
    --insecure-no-signature --state-dir ~/.oss-bridge/state
```

Keep it alive with the [systemd unit](examples/oss-bridge-runner.service).

**Local machine** — register the MCP server with your agent (next section), then ask it to
call `remote_hosts` and `remote_run`.

## Codex setup

Add to `~/.codex/config.toml`:

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

Running from a checkout instead: `command = "python3"`,
`args = ["-m", "ossbridge.mcp_server", ...]`, plus `cwd = "/path/to/oss-bridge"`.

Restart Codex and ask it to list the remote machines. Any other MCP client works the same
way — point it at the same stdio command.

Two things worth knowing:

- `remote_run` waits `wait_s` seconds (default 20), then returns a `job_id` you can poll
  with `remote_status`. Long builds should always go that route.
- The signing flag must match on both sides, or every request is rejected.

## Tools

| Tool | Arguments | What it does |
|---|---|---|
| `remote_hosts` | — | List registered machines, online state, GPU, in-flight jobs |
| `remote_run` | `host`, `cmd`, `cwd?`, `timeout_s?`, `wait_s?` | Run a shell command on a remote host |
| `remote_status` | `host`, `job_id` | Poll a job, including progress for long tasks |
| `remote_upload` | `host`, `local_path`, `remote_path` | Copy a local file to the remote host |
| `remote_download` | `host`, `remote_path`, `local_path` | Copy a remote file back |
| `remote_output` | `host`, `job_id`, `stream?` | Fetch truncated stdout/stderr from OSS |

`host` accepts a full `host_id`, a hostname, or any unique prefix of either.

## Configuration

Both sides take `--endpoint`, `--bucket` and `--prefix` (env:
`OSS_BRIDGE_ENDPOINT` / `OSS_BRIDGE_BUCKET` / `OSS_BRIDGE_PREFIX`), plus credentials from
`--ossutil-config`, `--sts-token-file`, or `--ak/--sk`. Precedence is CLI → env → TOML file,
so `--config examples/bridge.toml` works on both sides.

Templates: [bridge.env](examples/bridge.env.example) ·
[bridge.toml](examples/bridge.toml) · [ram-policy.json](examples/ram-policy.json)

## Security

A runner is a remote shell: anyone who can write to `inbox/` can ask it to execute a
command. Request signing is **on by default** — each request carries an HMAC-SHA256
signature and the key never touches the bucket, closing the gap between "can write to the
bucket" and "can execute commands on the hosts".

If the prefix is a personal directory only you can write to, pass
`--insecure-no-signature` on **both** sides and skip the key entirely. Requests also carry
a timestamp (replays older than 15 minutes are rejected) and are refused by any host other
than the one they were addressed to.

## Operations

- Add a bucket lifecycle rule for `files/`, `inbox/done/` and `outbox/result/`, otherwise
  the bucket grows forever.
- **Use the `-internal` endpoint.** Bucket-style public domains rely on wildcard DNS, which
  some container resolvers refuse.
- **Keep bucket versioning disabled**, or the `forbid-overwrite` claim primitive is ignored
  and two runners may execute the same job.
- Only single files are transferred — tar directories first.

OSS layout, the claim/lease protocol and troubleshooting:
[docs/internals.md](docs/internals.md).

## Self-updating deployment

Instead of copying code to every host, publish it to the bucket and re-run the bootstrap
script on the remote side whenever you want the latest code:

```bash
python3 scripts/publish_src.py --ossutil-config ~/.oss-bridge/ossutilconfig \
    --endpoint oss-cn-hangzhou-internal.aliyuncs.com \
    --bucket your-bucket --prefix oss-bridge
```

See [docs/internals.md](docs/internals.md) for the remote side.

## Development

Tests run against a real bucket, clean up after themselves, and skip without credentials.
Set `OSS_BRIDGE_TEST_ENDPOINT`, `OSS_BRIDGE_TEST_BUCKET`, `OSS_BRIDGE_TEST_PREFIX` and
`OSS_BRIDGE_TEST_OSSUTIL_CONFIG`, then:

```bash
python3 test/test_local_loop.py        # exec, async, timeout, files, claims (16 checks)
python3 test/test_mcp_stdio.py         # MCP handshake and tool calls (8 checks)
python3 test/test_remote_workflow.py   # publish -> bootstrap -> run (7 checks)
```

## License

MIT — see [LICENSE](LICENSE).
