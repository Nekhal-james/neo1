# model_conn — the off-board LLM host/receiver link

One CLI, `neo`, used on both ends of the Qwen 2.5 3B connection described in
[the implementation plan](../../docs/IMPLEMENTATION_PLAN.md), Phase 6:

| Role | Machine | Command |
|---|---|---|
| Host | the laptop serving the model | `neo --model path/to/model.gguf up` |
| Receiver | the Pi | `neo --connection:status`, `neo --connection:ping` |

## Quick start

On the laptop:

```bash
pip install -e ".[dev]"
neo --model /path/to/qwen2.5-3b-instruct-q4_k_m.gguf up
```

`up` requires [Ollama](https://ollama.com) to already be installed — this
package manages it (`ollama serve` + registering the model), it doesn't
replace it. Omit `--model` to use `host.default_model_path` from
`config/model_conn.yaml`.

On the Pi:

```bash
pip install -e ".[dev]"
neo --connection:status
neo --connection:ping
```

Both read `config/model_conn.yaml`'s `receiver.endpoints` (Ethernet first,
then Wi-Fi, per plan section 0.3.2) and write the result to a local status
file that `neo_webapp` reads — see below.

## Why Ollama, and only Ollama

Qwen 2.5 3B on a laptop GPU is squarely in Ollama's comfort zone: trivial
setup, GGUF quantization, an OpenAI-compatible endpoint, and a cheap
`/api/tags` liveness route this package uses for health checks. There is
exactly one model and one host in this project, so a pluggable backend
abstraction would be unused generality — `ollama.py` is the one place that
would need to change if that ever stops being true.

## Security: mTLS is stubbed, not skipped

CLAUDE.md and the plan (section 0.3.1) are explicit that an unauthenticated
inference endpoint on a campus network is an open proxy, and must never run —
not even briefly for testing. This first pass does not yet implement the
private-CA / certificate flow, and it does not pretend to: every `up`,
`--connection:status`, and `--connection:ping` invocation logs a loud,
impossible-to-miss warning (see `model_conn/tls.py`). Real cert loading raises
`TlsNotImplemented` rather than silently no-opping. Treat the warning as a
standing TODO, not acceptable steady state.

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

`status_store.py` writes an atomic (tempfile + `os.replace`) JSON file at
`var/model_conn/status.json` after every `up`/`status`/`ping` run.
`neo_webapp/link_status.py` reads it and serves `GET /api/link/status`
(authenticated) — so the panel shows real connection stats with **no ROS
installed at all**, matching how both `neo_webapp` and `neo_perception`
already run without a Pi.

Once `neo_msgs` lands (Phase 1), `nodes/link_node.py` becomes the ROS-native
path: a thin wrapper (same shape as `neo_perception`'s node) that drives the
identical `LinkTracker` on a timer and publishes `/link/health`, while still
writing the same status file so the two delivery paths never disagree.

## Layout

```
config.py       Config dataclass, load/merge pattern shared with neo_webapp
link.py         pure Python: Endpoint probing, LinkTracker, ping aggregation
ollama.py       host role: subprocess-manage `ollama serve` + model loading
status_store.py atomic JSON status file, read by neo_webapp
tls.py          stubbed mTLS -- loud warnings, no silent insecurity
nodes/          thin optional ROS wrapper (Phase 1)
```

## Running it

```bash
python -m pytest -q
```

Covers `link.py`'s probing/failover/ping-aggregation logic, the status file's
atomic write/read (including corrupt-file and missing-file handling), config
loading/merging, and `ollama.py`'s subprocess management (all monkeypatched —
no real Ollama binary required to run the suite).
