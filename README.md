# oss-bridge

**Give your coding agent a remote shell — without SSH, open ports, or a public IP.**

oss-bridge is an [MCP](https://modelcontextprotocol.io) server that turns an Alibaba Cloud
OSS bucket into a job queue plus a file staging area. A local agent (Codex, Claude Code,
Cursor, …) submits a command; a small daemon on the remote machine polls the bucket,
executes it, and writes the result back.

```
┌─────────────┐    MCP     ┌───────────────────────────────┐   polling   ┌──────────────┐
│  Local      │◄──────────►│           OSS bucket          │◄───────────►│ Remote host  │
│  agent      │            │                               │             │              │
│             │            │  registry/   machine heartbeat │             │ oss-bridge-  │
│ oss-bridge- │            │  hosts/<id>/inbox   commands   │             │ runner       │
│ agent       │            │  hosts/<id>/outbox  results    │             │              │
│ (MCP server)│            │  files/<job>/       big files  │             │ (daemon)     │
└─────────────┘            └───────────────────────────────┘             └──────────────┘
```

中文文档见 [README.zh-CN.md](README.zh-CN.md)。

## Why

Plenty of machines you want an agent to drive are unreachable: private VPC boxes, GPU
workers behind NAT, appliances where you cannot open a port or install an SSH server.
What they often *do* have is outbound HTTPS to object storage.

oss-bridge uses that one channel for everything:

* **No inbound access.** The remote machine only makes outbound requests to OSS.
* **No extra infrastructure.** No broker, no gateway process, no VPN.
* **Files too.** Big command output and input files travel through the same bucket, so a
  2 GB tarball never has to pass through the model's context window.
* **Multi-host.** Every runner registers itself; the agent addresses hosts by id,
  hostname, or a unique prefix.

The trade-off is latency: everything is store-and-forward, so a round trip takes roughly
one poll interval (1–3 seconds by default). It is built for *submit a job, fetch the
result* — not for interactive or streaming sessions.

## Requirements

* Python **3.10+** (both sides)
* An Alibaba Cloud OSS bucket and credentials with read/write access to one prefix
* `oss2` (installed automatically as a dependency)

## Install

```bash
pip install oss-bridge          # once published to PyPI

# or from a checkout
git clone https://github.com/OWNER/oss-bridge && cd oss-bridge
pip install -e .
```

This provides two console scripts: `oss-bridge-agent` (local MCP server) and
`oss-bridge-runner` (remote daemon). Running from the checkout with
`python3 -m ossbridge.mcp_server` / `python3 -m ossbridge.runner` works too.

## Quick start

**1. Remote host** — put credentials and a config file in place, then start the runner
(see [Configure the remote machine](#configure-the-remote-machine-runner) for the
one-time setup):

```bash
python3 -m ossbridge.runner --ossutil-config ~/.oss-bridge/ossutilconfig \
    --endpoint oss-cn-hangzhou-internal.aliyuncs.com \
    --bucket your-bucket --prefix oss-bridge \
    --insecure-no-signature --state-dir ~/.oss-bridge/state
```

**2. Local machine** — register the MCP server with your agent (see
[Codex setup](#codex-setup) below).

**3. Ask your agent** to call `remote_hosts`, then `remote_run`.

## Configure the local machine (MCP server)

`oss-bridge-agent` speaks MCP over stdio. Configure it with CLI flags or environment
variables:

| Flag | Env var | Meaning |
|---|---|---|
| `--endpoint` | `OSS_BRIDGE_ENDPOINT` | OSS endpoint, e.g. `oss-cn-hangzhou-internal.aliyuncs.com` |
| `--bucket` | `OSS_BRIDGE_BUCKET` | Bucket name |
| `--prefix` | `OSS_BRIDGE_PREFIX` | Working prefix inside the bucket (default `oss-bridge/`) |
| `--ossutil-config` | — | ossutil config file to read endpoint/AK/SK from |
| `--sts-token-file` | — | STS credentials JSON |
| `--ak` / `--sk` | `OSS_BRIDGE_AK` / `OSS_BRIDGE_SK` | Credentials given directly |
| `--secret-file` | — | File holding the shared HMAC key (enables request signing) |
| `--insecure-no-signature` | — | Disable request signing (see [Security](#security)) |
| `--config` | — | TOML config file (`examples/bridge.toml`) |

Precedence: CLI flags → environment variables → config file → built-in defaults.

## Configure the remote machine (runner)

The runner needs a way to reach OSS (credentials + endpoint), the same bucket and prefix
as the local side, and a state directory where it remembers its `host_id`.

Credential sources are tried in this order: `--ak/--sk`, `--ossutil-config`,
`--sts-token-file`. Environment equivalents exist for all of them. A convenient pattern:

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

### Registering starts automatically

On first start the runner derives a stable `host_id` from the hostname plus a hash of
machine-id/MAC (for example `gpu-node-01-3f9a2c`), persists it under `--state-dir`, and
writes `registry/<host_id>.json` every 15 seconds. The id never changes afterwards —
renaming a live host would orphan commands that are already queued. If two hosts would
collide, the later one quietly becomes `-2`, `-3`, … The local agent resolves each `host`
argument against the registry, so a hostname, a full `host_id`, or a unique prefix all
work.

### Run it as a service

See [`examples/oss-bridge-runner.service`](examples/oss-bridge-runner.service) for a
systemd unit with sane hardening. Run the daemon as a dedicated low-privilege user — it
executes shell commands by design.

Multiple runners on one machine (different projects or users) are supported: give each one
`--instance <name>` and a separate `--state-dir`.

## Codex setup

Add the server to `~/.codex/config.toml`:

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

Running from a checkout instead:

```toml
[mcp_servers.oss_bridge]
command = "python3"
args = ["-m", "ossbridge.mcp_server", "--endpoint", "…", "--bucket", "…", "--prefix", "…",
        "--insecure-no-signature"]
cwd = "/path/to/oss-bridge"
```

Restart Codex, then ask it to list the remote machines — it should call `remote_hosts`
and show the runner you started earlier.

Two practical notes:

* **Tool timeouts.** `remote_run` waits `wait_s` seconds (default 20) and then returns a
  `job_id` instead of blocking forever. Tell your agent it can poll with `remote_status`.
  Long builds and training jobs should always go that route.
* **Signing flags must match.** If the runner verifies signatures, the MCP server must
  sign; if one side disables it, requests are rejected. The same applies to the
  `--secret-file` contents.

Any MCP client works the same way (Claude Code, Cursor, custom clients) — point it at the
stdio command.

## Tools

| Tool | Arguments | What it does |
|---|---|---|
| `remote_hosts` | — | List registered machines with online state, GPU, in-flight jobs |
| `remote_run` | `host`, `cmd`, `cwd?`, `timeout_s?`, `wait_s?` | Run a shell command on a remote host |
| `remote_status` | `host`, `job_id` | Poll a job; includes progress written by the runner |
| `remote_upload` | `host`, `local_path`, `remote_path`, `wait_s?` | Copy a local file to the remote host |
| `remote_download` | `host`, `remote_path`, `local_path`, `wait_s?` | Copy a remote file back |
| `remote_output` | `host`, `job_id`, `stream?`, `max_bytes?` | Fetch truncated stdout/stderr from OSS |

Results report `status` (`ok` / `failed` / `timeout` / `rejected`), `exit_code`,
`duration_ms`, and `artifacts`. Output above `max_output_bytes` is uploaded to
`files/<job>/` and only a head/tail summary is returned inline.

## OSS layout

```
<prefix>/
  registry/<host_id>.json                     runner heartbeat (hostname, GPU, load, version)
  registry/<host_id>/owner.json               identity lease, resolves host_id collisions
  hosts/<host_id>/inbox/pending/<job>.json    commands   (agent writes -> runner reads)
  hosts/<host_id>/inbox/claimed/<job>.json    atomic claim (forbid-overwrite)
  hosts/<host_id>/inbox/done/<job>.json       processed marker
  hosts/<host_id>/outbox/result/<job>.json    results    (runner writes -> agent reads)
  hosts/<host_id>/outbox/progress/<job>.json  live progress for long jobs
  files/<job>/...                             staged inputs, oversized outputs
  src/...                                     optional self-update payload
```

Job object names start with a millisecond timestamp so the runner can page through new
work with `start_after`, with a periodic full sweep as a safety net.

### How a command flows

1. The agent optionally signs the request and `PutObject`s it into `inbox/pending/`.
2. The runner lists that prefix, reads the request, and creates `inbox/claimed/<job>`
   using `x-oss-forbid-overwrite: true` — a conditional PUT that fails with `409` if
   another runner got there first. **This requires bucket versioning to be disabled**;
   with versioning on, the header is ignored and the primitive is lost.
3. The runner executes the command in its own process group with a timeout that kills the
   whole group, then writes `outbox/result/<job>`.
4. It writes a `done` marker and deletes the pending/claimed objects.

If a runner dies mid-job, the claim's lease expires (`timeout_s + lease_grace_s`) and the
request is re-published with `attempt + 1`; after `max_attempts` it becomes a dead letter
(a `failed` result). An existing result object always wins — the same job id is never
executed twice.

## Security

**A runner is a remote shell.** Treat the prefix as a privileged resource: anyone who can
write into `inbox/pending/` can ask the runner to execute a command.

Request signing is **on by default**. When enabled, every request carries an HMAC-SHA256
signature over its payload, and the key (`--secret-file`) lives only in local
configuration on both sides — never in the bucket. That closes the gap between "can write
to the bucket" and "can execute commands on the hosts".

If the prefix is a personal directory that only you can write to, you can drop the extra
key with `--insecure-no-signature` (set it on **both** sides). You are trading one secret
for reliance on your bucket ACL — a reasonable trade when the ACL really is just you.

Other defaults worth knowing:

* Requests carry a timestamp; anything outside ±15 minutes is rejected as a replay.
* A request addressed to `host_id` X is refused by host Y.
* Each job gets its own working directory, and timeouts kill the entire process group.
* [`examples/ram-policy.json`](examples/ram-policy.json) sketches a least-privilege RAM
  policy for both sides.

## Operations

* **Latency.** One round trip is roughly one poll interval: 2 s idle, 0.3 s while work is
  flowing.
* **Cost.** Polling is `ListObjects` traffic; idle back-off keeps it small.
* **Cleanup.** Add a bucket lifecycle rule for `files/`, `inbox/done/`, and
  `outbox/result/` (expire after 7 days, say) or the bucket grows forever.
* **Big files.** A single `PutObject` is capped at 5 GB; `oss2` handles multipart
  automatically beyond that. Only single files are supported — tar a directory first.

### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `No address associated with hostname` on a public endpoint | Bucket-style public domains rely on wildcard DNS, which some container resolvers refuse. Use the `-internal` endpoint. |
| `InvalidArgument: max-keys must be an integer between 1 and 1000` | OSS caps `max-keys` at 1000; `store.list_keys` clamps it automatically. |
| Claiming never succeeds | The bucket has versioning enabled, which disables `x-oss-forbid-overwrite`. |
| Everything comes back `rejected` | Signing flags differ between the two sides, or the secrets do not match. |
| Jobs execute twice | Two runners share one `host_id`; give each `--instance` or a distinct state dir. |

## Optional: self-updating deployment

Instead of copying code to each remote machine, publish it to the bucket and let a small
bootstrap script keep runners current:

```bash
# local machine: package the checkout and upload it
python3 scripts/publish_src.py --ossutil-config ~/.oss-bridge/ossutilconfig \
    --endpoint oss-cn-hangzhou-internal.aliyuncs.com \
    --bucket your-bucket --prefix oss-bridge
```

This writes `src/oss-bridge-src.tar.gz`, `src/latest.json` (sha256 + file list),
`src/releases/<timestamp>-<sha8>.tar.gz` (last 5 kept for rollback), and
`src/remote_run.sh`. Packaging is deterministic, so an unchanged tree is skipped.

On the remote machine, fetch the bootstrap script once and run it whenever you want the
latest code:

```bash
ossutil64 cp oss://your-bucket/oss-bridge/src/remote_run.sh ./
bash remote_run.sh                       # reads ~/.oss-bridge/bridge.env
```

`remote_run.sh` compares `latest.json` against the local cache, downloads and verifies the
package when needed, switches `~/.oss-bridge/current`, and execs the runner. Re-running it
is the entire update procedure; see
[`examples/bridge.env.example`](examples/bridge.env.example).

## Development

```
ossbridge/
  config.py       configuration merging (CLI -> env -> file -> defaults)
  naming.py       host_id derivation, machine info, heartbeats
  protocol.py     request/result envelopes and HMAC signing
  store.py        the four OSS calls + the atomic claim primitive
  runner.py       remote daemon
  client.py       local-side client used by both the MCP server and the tests
  mcp_server.py   stdio MCP server (minimal subset, no SDK dependency)
scripts/
  publish_src.py  package + upload the checkout
  remote_run.sh   pull the latest code and start the runner
test/             end-to-end tests against a real bucket
```

Tests talk to a real OSS bucket and clean up after themselves. They skip when no test
credentials are configured:

```bash
export OSS_BRIDGE_TEST_ENDPOINT=oss-cn-hangzhou-internal.aliyuncs.com
export OSS_BRIDGE_TEST_BUCKET=your-test-bucket
export OSS_BRIDGE_TEST_PREFIX=oss-bridge-selftest/
export OSS_BRIDGE_TEST_OSSUTIL_CONFIG=~/.oss-bridge/ossutilconfig

python3 test/test_local_loop.py                  # 16 checks: exec, async, timeout, files, claims
python3 test/test_mcp_stdio.py                   # 8 checks: MCP handshake and tool calls
python3 test/test_remote_workflow.py             # 7 checks: publish -> bootstrap -> run
python3 test/test_local_loop.py --no-signature   # same suite without request signing
```

Design notes live in the module docstrings — `runner.py` in particular documents the
claim/lease/retry protocol.

## License

MIT — see [LICENSE](LICENSE).
