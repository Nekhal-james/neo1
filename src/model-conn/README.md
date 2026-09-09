# model_conn — the off-board LLM host/receiver link

One CLI, `neo`, used on both ends of the Qwen 2.5 3B connection described in
[the implementation plan](../../docs/IMPLEMENTATION_PLAN.md), Phase 6:

| Role | Machine | Command |
|---|---|---|
| Host | the laptop serving the model | `neo --model path/to/model.gguf up` |
| Receiver | the Pi | `neo --connection:status`, `neo --connection:ping` |
| Either | reads what the camera sees | `neo --vision:status` |

## Quick start

Install from the repo root — `pip install -e ".[dev]"` — which installs this
package alongside `neo_webapp` and `neo_perception` in one shot; see the
[top-level README](../../README.md). There is no separate install for this
package alone: `neo --webapp up` (below) needs `neo_webapp` importable, so
even on a Pi that only ever runs `--connection:status`/`--connection:ping`,
install from the repo root.

On the laptop:

```bash
neo --model /path/to/qwen2.5-3b-instruct-q4_k_m.gguf up
```

`up` requires [Ollama](https://ollama.com) to already be installed — this
package manages it (`ollama serve` + registering the model), it doesn't
replace it. Omit `--model` to use `host.default_model_path` from
`config/model_conn.yaml`.

On the Pi:

```bash
neo --connection:status
neo --connection:ping
```

Both read `config/model_conn.yaml`'s `receiver.endpoints` (Ethernet first,
then Wi-Fi, per plan section 0.3.2) and write the result to a local status
file that `neo_webapp` reads — see below.

`neo` also forwards to the admin panel itself — `neo --webapp {up,setup,devcert}`
(see [neo_webapp's README](../neo_webapp/README.md)) — this only works when
`neo_webapp` is installed alongside this package, which the root-level
install does. Every flag after the subcommand that `neo` itself doesn't
recognize is forwarded to `neo_webapp`'s own CLI untouched, **including a
`--config`** — `neo --webapp up --config panel.yaml` passes `panel.yaml` to
`neo_webapp`, not to this package (which has its own, separate `--config` for
`up`/`--connection:status`/`--connection:ping`/`--prompt`, described below).

To test the assistant directly (see
[intelligence's README](../intelligence/README.md)):

```bash
neo --prompt "where is CS-204"
```

Exactly one mode may be selected per invocation — `up`, `--connection:status`,
`--connection:ping`, `--vision:status`, or `--prompt` — combining two of them is
a usage error, not silently-picked precedence.

To see what the robot is currently looking at:

```bash
neo --vision:status
```

This reads [`neo_perception`](../neo_perception/README.md)'s status file rather
than opening a camera of its own — the camera belongs to whichever process is
running perception (today `neo --webapp up`). It mirrors `--connection:status`,
which reads this package's own. Three distinct outcomes, deliberately not
collapsed: `UP` (live), `DOWN` (panel running, no detector), and `STALE` (nothing
is running).

Set up mTLS once (see below), then flip it on:

```bash
neo --tls init   # generates a private CA + server cert (this machine) + client cert (the receiver)
```

## Why Ollama, and only Ollama

Qwen 2.5 3B on a laptop GPU is squarely in Ollama's comfort zone: trivial
setup, GGUF quantization, an OpenAI-compatible endpoint, and a cheap
`/api/tags` liveness route this package uses for health checks. There is
exactly one model and one host in this project, so a pluggable backend
abstraction would be unused generality — `ollama.py` is the one place that
would need to change if that ever stops being true.

## Security: real mTLS, via a private CA

CLAUDE.md and the plan (section 0.3.1) are explicit that an unauthenticated
inference endpoint on a campus network is an open proxy, and must never run —
not even briefly for testing. `neo --tls init` (`certs.py`) generates a
private CA once, a server cert for this machine (SAN covers `localhost`,
this machine's hostname/IPs, and every host in `receiver.endpoints` — so
both the Ethernet address and the Wi-Fi hostname validate), and a client cert
for the receiver. Re-running `init` never touches the CA again (so
previously-issued certs stay valid) but reissues the server/client certs —
useful after adding a new endpoint.

**Deploying to two machines:** run `neo --tls init` on the host. Copy
`certs/model_conn/{ca-cert.pem,client-cert.pem,client-key.pem}` to the same
path on the Pi — the server cert/key never leave the host. Set
`tls.enabled: true` in `config/model_conn.local.yaml` **on both machines**.

**Why a proxy in front of Ollama:** Ollama has no native TLS or
client-certificate support. `neo --model ... up` binds Ollama itself to
`127.0.0.1:{host.internal_ollama_port}` (loopback-only, never reachable from
the network) and `tls_proxy.py` — a small FastAPI/uvicorn app, already a hard
dependency via `neo_webapp` so this adds nothing new — takes the public
`bind_host:ollama_port` with `ssl_cert_reqs=CERT_REQUIRED`, forwarding only
requests that present a certificate signed by the CA.

`tls.enabled: false` is a supported, explicit opt-out for local/dev testing
(e.g. everything on one laptop loopback) — every `up`, `--connection:status`,
and `--connection:ping` invocation logs a loud, impossible-to-miss warning in
that case (`tls.warn_insecure`), so it can never be silently insecure. If
`tls.enabled` is true but the certs are missing, commands fail with a clear
"run `neo --tls init` first" — never a stack trace, never a silent fallback
to plaintext.

`tests/test_tls_proxy.py` proves the whole thing with a real TLS handshake
(no mocks): a valid client cert succeeds, no cert is rejected, and a cert
signed by a *different* CA is rejected too — not just "any cert works."

## Two grace-free failure surfaces, on purpose

`link.py` is pure Python — the only real I/O is behind a `transport`
parameter (an HTTP GET against `/api/tags`), so `LinkTracker` and `run_ping`
are unit-tested with a fake transport and no socket, the same discipline
`neo_perception`'s core uses for an explicit clock.

- `probe_ordered` tries Ethernet, then Wi-Fi, first success wins — matching
  the plan's dual-path failover order. If your campus Wi-Fi has AP isolation
  enabled, the Wi-Fi path will simply never succeed; that's expected, not a
  bug in this module.
- `LinkTracker` carries `consecutive_failures` across calls, which is what
  `--connection:status` and (later) the continuous ROS node need in common.

## Getting stats into the admin panel

`status_store.py` writes two atomic (tempfile + `os.replace`) JSON files, and the
split matters — they answer different questions and only one of them can be
answered from each end:

| File | Written by | Answers | Read by |
|---|---|---|---|
| `var/model_conn/status.json` | the **receiver** (`--connection:status`/`ping`) | can I reach the host, over which path, at what latency | `GET /api/link/status` |
| `var/model_conn/host.json` | the **host** (`--model … up`) | is a model up *right now*, which one, and where | `GET /api/model/status` |

The host file is the one no amount of probing from the receiver side can produce.
Its most useful field is the **model name Ollama actually registered**, because
asking for the wrong name fails as a 404 that looks exactly like the link being
down — so the panel compares it against `chat.model_name` and says so out loud.

Two properties it deliberately has:

- **It heartbeats** (every 5 s while `up` runs) rather than being written once.
  A one-shot file keeps claiming a model is up long after the process was killed,
  and killed is how a foreground `up` normally ends. Readers treat anything older
  than 20 s as "not running".
- **A clean Ctrl-C writes `serving: false`**, so a deliberate shutdown shows in
  the panel immediately instead of after the staleness timeout.

Both are read with **no ROS installed at all**, matching how `neo_webapp` and
`neo_perception` already run without a Pi.

Once `neo_msgs` lands (Phase 1), `nodes/link_node.py` becomes the ROS-native
path: a thin wrapper (same shape as `neo_perception`'s node) that drives the
identical `LinkTracker` on a timer and publishes `/link/health`, while still
writing the same status file so the two delivery paths never disagree.

## Layout

```
config.py       Config dataclass, load/merge pattern shared with neo_webapp
link.py         pure Python: Endpoint probing, LinkTracker, ping aggregation
ollama.py       host role: subprocess-manage `ollama serve` + model loading
certs.py        private CA + server/client cert generation (neo --tls init)
tls.py          cert/context helpers -- scheme, client cert kwargs, server ssl kwargs
tls_proxy.py    the mTLS-terminating reverse proxy in front of Ollama
status_store.py atomic JSON status file, read by neo_webapp
nodes/          thin optional ROS wrapper (Phase 1)
```

## Running it

```bash
python -m pytest -q
```

Covers `link.py`'s probing/failover/ping-aggregation logic, the status file's
atomic write/read (including corrupt-file and missing-file handling), config
loading/merging, and `ollama.py`'s subprocess management (all monkeypatched —
no real Ollama binary required to run the suite). `certs.py`'s CA/cert
generation is checked structurally (issuer, EKU, SAN, key-identifier chaining)
with `cryptography`; `tls_proxy.py` is proven with **real TLS handshakes** —
an actual server, actual sockets, actual OpenSSL — the one place in this
package where a mock would hide the bug that actually matters.
