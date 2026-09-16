# Internals & operations

Details behind [README.md](../README.md): the bucket layout, the claim/lease protocol, the
self-updating deployment flow and troubleshooting.

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

Everything is per-host, so two runners never compete for the same queue. Job object names
start with a millisecond timestamp, which lets a runner page through new work with
`start_after`; a full sweep every `full_sweep_interval` seconds catches anything that
sorted below the cursor.

## How a command flows

1. The agent optionally signs the request and `PutObject`s it into `inbox/pending/`.
2. The runner lists that prefix, reads the request, and creates `inbox/claimed/<job>` using
   `x-oss-forbid-overwrite: true` — a conditional PUT that fails with `409` if another
   runner got there first. **This requires bucket versioning to be disabled**; with
   versioning on the header is ignored and the primitive is lost.
3. The runner executes the command in its own process group, with a timeout that kills the
   whole group, then writes `outbox/result/<job>`.
4. It writes a `done` marker and deletes the pending/claimed objects.

While a long job runs, the runner rewrites `outbox/progress/<job>` every few seconds with
the elapsed time and the tail of stdout, so an agent polling `remote_status` can tell the
difference between "still working" and "stuck".

### Failure handling

Evidence of at-least-once semantics, and how it is contained:

| Situation | What happens |
|---|---|
| Result object already exists | The pending object is deleted without executing; a job id never runs twice |
| Runner dies mid-job | The claim lease expires (`timeout_s + lease_grace_s`) and the request is republished with `attempt + 1` |
| Too many attempts | After `max_attempts` the job becomes a dead letter: the runner writes a `failed` result instead of retrying |
| Command exceeds `timeout_s` | The whole process group is killed (SIGTERM, then SIGKILL) and the result has `status=timeout` |
| Output exceeds `max_output_bytes` | The full stream is uploaded to `files/<job>/`; the inline result keeps a head/tail summary plus the object key |

## Host identity

On first start a runner derives a `host_id` from the hostname plus a hash of
machine-id/MAC (for example `gpu-node-01-3f9a2c`) and persists it under `--state-dir`. It
is never renamed afterwards — renaming a live host would orphan jobs that are already
queued in its prefix. If two hosts would collide, the later one quietly becomes `-2`,
`-3`, … The local side resolves each `host` argument against the registry, so a hostname,
a full `host_id`, or a unique prefix all work.

Multiple runners on one machine: give each `--instance <name>` and a separate `--state-dir`.

## Operations

* **Latency.** One round trip is roughly one poll interval: 2 s idle
  (`poll_interval_idle`), 0.3 s while work is flowing (`poll_interval_active`).
* **Cost.** Polling is `ListObjects` traffic. Back-off when idle keeps it small; a
  millisecond-timestamp naming scheme keeps each list short.
* **Cleanup.** Add a bucket lifecycle rule for `files/`, `inbox/done/` and
  `outbox/result/` (expire after 7 days, say). Nothing is deleted automatically otherwise.
* **Size limits.** A single `PutObject` is capped at 5 GB; `oss2` switches to multipart
  automatically beyond that. Only single files are supported — tar a directory first.
* **Concurrency.** `concurrency` (default 2) caps how many jobs a runner executes at once;
  each job gets its own working directory under `--work-dir`.

## Self-updating deployment

Publish the checkout to the bucket:

```bash
python3 scripts/publish_src.py --ossutil-config ~/.oss-bridge/ossutilconfig \
    --endpoint oss-cn-hangzhou-internal.aliyuncs.com \
    --bucket your-bucket --prefix oss-bridge
```

This writes:

| Object | Purpose |
|---|---|
| `src/oss-bridge-src.tar.gz` | Latest package (overwritten each publish) |
| `src/latest.json` | sha256, size, file list, upload time |
| `src/releases/<timestamp>-<sha8>.tar.gz` | History, last 5 kept for rollback |
| `src/remote_run.sh` | Bootstrap script |

Packaging is deterministic (sorted entries, zeroed mtime/uid/gid, fixed gzip header), so an
unchanged tree produces the same sha256 and is skipped.

On the remote host, fetch the script once:

```bash
ossutil64 cp oss://your-bucket/oss-bridge/src/remote_run.sh ./
bash remote_run.sh
```

`remote_run.sh` reads `~/.oss-bridge/bridge.env` for defaults, compares `latest.json`
against its local cache, downloads and verifies the package when needed, switches
`~/.oss-bridge/current`, and execs the runner. Re-running it is the whole update procedure.
Set `OSS_BRIDGE_NO_UPDATE=1` to start from the cache without touching the bucket.

It also probes credentials in order — `OSS_BRIDGE_STS_TOKEN_FILE`, `/fuyao_oss_sts/token`
(if present), `OSS_BRIDGE_OSSUTIL_CONFIG`, `~/.oss-bridge/ossutilconfig`, then
`OSS_BRIDGE_AK`/`OSS_BRIDGE_SK` — and uses the first one that can read the bucket, which is
handy on machines where platform-injected STS tokens do not cover your bucket.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `No address associated with hostname` on a public endpoint | Bucket-style public domains rely on wildcard DNS, which some container resolvers refuse. Use the `-internal` endpoint. |
| `Job ... rejected` for everything | Signing flags differ between the two sides, or the secrets do not match. |
| Claiming never succeeds | Bucket versioning is enabled, which disables `x-oss-forbid-overwrite`. |
| `InvalidArgument: max-keys must be an integer between 1 and 1000` | OSS caps `max-keys` at 1000; `store.list_keys` clamps it automatically. |
| A job runs twice | Two runners share one `host_id`; give each `--instance` or a distinct state dir. |
| `remote_hosts` shows nothing | The runner never reached OSS: check credentials, bucket and prefix on the remote side. |
| Runner keeps restarting after an STS token expires | Long-lived daemons need long-lived credentials; STS tokens expire (typically in 24 h). |

## Design notes

The code is deliberately small and dependency-light: `oss2` is the only runtime
dependency, and the MCP stdio server implements the minimal subset
(`initialize` / `tools/list` / `tools/call` / `ping`) instead of pulling in an SDK. Module
docstrings document the invariants — `runner.py` in particular covers the claim, lease and
retry protocol.
