# intelligence — prompts, RAG data, chat/ASR/TTS

What the assistant says and how it hears/speaks, reached through
[`model_conn`](../model-conn/README.md)'s link rather than talking to the
off-board host directly. Implements parts of Phases 5/6/7 of
[the implementation plan](../../docs/IMPLEMENTATION_PLAN.md), ahead of the
plan's own `neo_dialog`/`neo_kb` ROS packages landing.

## Quick start

Install from the repo root — `pip install -e ".[dev,voice]"` — see the
[top-level README](../../README.md). `voice` (Vosk + Piper) is optional; the
chat path (`neo --prompt`) works without it.

Test the assistant directly, e.g. on the Pi:

```bash
neo --prompt "where is CS-204"
```

This resolves the healthy off-board endpoint via `model_conn`'s link
(Ethernet, then Wi-Fi, then localhost as a same-machine dev fallback), sends
your text plus the system prompt to Ollama's chat API, and prints the reply.
With no reachable endpoint it prints a degraded-mode message and exits
non-zero — it never crashes, per CLAUDE.md's "degraded mode is normal."

## Layout

```
intelligence/
  config.py          Config.load(), config/intelligence.yaml + .local.yaml
  prompts.py         load_system_prompt() -- reads a file, not a string literal
  prompts/
    system_prompt.txt  placeholder -- fill in the real persona/rules later
  chat.py            ask() -- resolves model_conn's link, calls Ollama's chat API
  asr.py             transcribe() -- local Vosk speech-to-text
  tts.py             synthesize() -- local Piper text-to-speech
  status_store.py    atomic JSON status file, read by neo_webapp
  cli.py             run_prompt() -- what `neo --prompt` calls
  nodes/             thin optional ROS wrapper (Phase 1/5)
rag/
  data/              placeholder -- campus classroom/location data arrives here (Phase 7)
```

## Why chat.py doesn't duplicate model_conn

`chat.ask()` reuses `model_conn.link.probe_ordered` and
`model_conn.config.Config` directly to find the live Ollama host — the exact
same eth-then-wifi resolution `--connection:status`/`--connection:ping`
already use. It never re-implements probing or health checks; it just adds
one more thing you can do once a healthy endpoint is found (POST a chat
request instead of just checking `/api/tags`).

This also means `neo --prompt` picks up mTLS for free: `chat_transport_for()`
mirrors `model_conn.link.transport_for()` exactly (plain HTTP when
`tls.enabled` is false, HTTPS with the client cert when it's true — see
[model_conn's README](../model-conn/README.md)). Nothing in this package
knows or cares whether TLS is on; it just asks `model_conn` for the right
transport.

## Why Vosk and Piper, and why they're an optional extra

Vosk (STT) is always-local per CLAUDE.md's invariant that ASR runs on the Pi
regardless of whether the laptop is reachable — off-board Whisper is a
Phase 6 accuracy upgrade, not implemented here. Piper (TTS) is local by
design in the plan too. Both are real dependencies, not stubs, but they're
gated behind the `voice` extra (same reasoning as `neo_perception`'s
`detector` extra): `chat.py`'s LLM path works with nothing but `requests`,
so most development doesn't need Vosk/Piper installed at all.

Both `asr.transcribe()` and `tts.synthesize()` need a real downloaded model
file (a Vosk model directory, a Piper `.onnx` voice) configured in
`config/intelligence.local.yaml` — with no path configured or no file found,
they raise a clear error, the same tone as `model_conn.ollama.up()`'s error
handling, never a bare stack trace.

## The RAG placeholder

`rag/data/` is empty on purpose — see [`rag/README.md`](rag/README.md) for
the vectorless structure (`rooms.yaml`/`graph.yaml`/`coverage.yaml`) it will
hold once Phase 7 is built. No retrieval logic exists here yet.

## Surfacing state in the admin panel

Every `chat.ask()` call (so every `neo --prompt`) writes
`var/intelligence/status.json` — atomic tempfile-then-rename, same pattern as
`model_conn/status_store.py`. `neo_webapp` reads it with zero ROS installed
via `GET /api/dialog/status` (see `src/neo_webapp/neo_webapp/dialog_status.py`),
exactly how `/api/link/status` already surfaces `model_conn`'s state.

## Running it

```bash
python -m pytest -q
```

Covers `chat.ask()`'s endpoint resolution and degraded-mode fallback (fake
transports, no real network), `asr`/`tts`'s missing-dependency and
missing-model-path error paths (monkeypatched `vosk`/`piper` modules, no real
model files needed), prompt loading, and the status file's atomic
write/read.
