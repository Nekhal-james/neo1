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
  asr.py             Session (streaming) + transcribe() -- local Vosk speech-to-text
  tts.py             synthesize_pcm() / synthesize() -- local Piper text-to-speech
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

## Why Vosk and Piper, and why they're an optional extra

Vosk (STT) is always-local per CLAUDE.md's invariant that ASR runs on the Pi
regardless of whether the laptop is reachable — off-board Whisper is a
Phase 6 accuracy upgrade, not implemented here. Piper (TTS) is local by
design in the plan too. Both are real dependencies, not stubs, but they're
gated behind the `voice` extra (same reasoning as `neo_perception`'s
`detector` extra): `chat.py`'s LLM path works with nothing but `requests`,
so most development doesn't need Vosk/Piper installed at all.

Both need a real downloaded model file (a Vosk model directory, a Piper
`.onnx` voice) configured in `config/intelligence.local.yaml` — with no path
configured or no file found, they raise a clear error, the same tone as
`model_conn.ollama.up()`'s error handling, never a bare stack trace.
`availability()` on each module answers the same question *without* loading
anything, which is what lets the admin panel poll it and show the reason.

### Three things here that a thin wrapper would get wrong

**Models are cached.** Loading a Vosk model takes seconds, and it used to
happen on every `transcribe()` call. Survivable for a one-shot CLI invocation,
fatal for streaming, where audio arrives every ~20 ms. Same for Piper voices:
per-utterance loading would eat the whole 500 ms first-audio budget in
plan §12.2. `reset_cache()` exists so a config change at runtime does not keep
serving the old model.

**`asr.Session` streams.** Vosk decodes incrementally and reports its own
endpoints; `transcribe()` throws that away and makes the caller guess when
someone stopped talking. A fixed silence timer in the caller either clips
people who pause mid-sentence or makes everyone wait it out — so the
recognizer calls the endpoint, and `partial()` gives the running hypothesis
that makes a UI feel alive while you are still speaking.

**`tts.synthesize_pcm()` returns raw PCM at a rate you choose.** A WAV header
in the middle of a PCM stream is not something a speaker channel can do
anything with, and a voice at a rate the player was not built for is not an
error — it just plays at the wrong pitch, which is a genuinely confusing bug to
chase. The resampler is hand-rolled rather than `audioop.ratecv` because
`audioop` was removed from the standard library in Python 3.13.

## The RAG placeholder

`rag/data/` is empty on purpose — see [`rag/README.md`](rag/README.md) for
the vectorless structure (`rooms.yaml`/`graph.yaml`/`coverage.yaml`) it will
hold once Phase 7 is built. No retrieval logic exists here yet.

## Camera context: how "what is this?" becomes answerable

`chat.ask()` appends one line about what the camera currently sees to the
**system** prompt — never to the user's message, because it is situational
context, not something the person said:

> What your camera sees right now: 1 person in view (engaged); most recently
> identified object: cell phone.

It comes from [`neo_perception`](../neo_perception/README.md)'s status file, not
from a camera. That is the whole point: the camera belongs to whichever process
is running perception (today the admin panel), while this one may be a
short-lived `neo --prompt`. A missing or stale file yields no context, which is
the same as having no camera — so the feature can never cost you an answer.

Without it, "what am I holding" is unanswerable no matter how good either half
of the system is.

## Surfacing state in the admin panel

Every `chat.ask()` call writes `var/intelligence/status.json` — atomic
tempfile-then-rename, same pattern as `model_conn/status_store.py`. `neo_webapp`
reads it with zero ROS installed via `GET /api/dialog/status` (see
`src/neo_webapp/neo_webapp/dialog_status.py`), exactly how `/api/link/status`
already surfaces `model_conn`'s state.

That is now written from **two** surfaces, not one: `neo --prompt` and the
panel's Dialog tab, which posts to `/api/dialog/ask`. Both call this same
`ask()`, so endpoint resolution, mTLS, the degraded reply and the status write
cannot drift between them — and the "last chat turn" card reflects whichever
asked last.

## Running it

```bash
python -m pytest -q
```

Covers `chat.ask()`'s endpoint resolution and degraded-mode fallback (fake
transports, no real network), `asr`/`tts`'s missing-dependency and
missing-model-path error paths (monkeypatched `vosk`/`piper` modules, no real
model files needed), prompt loading, and the status file's atomic
write/read.
