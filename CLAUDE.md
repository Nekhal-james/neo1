# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

The repository is currently empty apart from `README.md` — there is no source code, build system, or test suite yet. Everything below is the intended design, provided by the project owner. When scaffolding, prefer creating the structure described here over inventing a new one, and update this file as real commands and layout appear.

The build order, package layout, and per-phase acceptance criteria live in [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md). Follow its phase ordering unless told otherwise, and keep it updated as phases land.

## Project: Neo

Neo is a receptionist robot for a college campus, running on a Raspberry Pi 4 (4GB). Two roles:

1. **Info desk** — answers questions about the campus (e.g. "where is classroom CS-204?", "how do I get to B block?") from college classroom/location data.
2. **General AI assistant** — conversational answers outside the info-desk domain.

It is head-only for now (pan/tilt), wake-word gated, and has a web app that is both the robot's admin panel and a stand-in for its physical I/O during development.

## Architecture (intended)

- **ROS 2 Jazzy** is the integration layer. Perception, LLM, motion, and audio are separate nodes communicating over topics/services, so any of them can be swapped for a simulated/web-app source without the others knowing.
- **LLM: Qwen 2.5 3B, off-board.** The model does not run on the Pi. The Pi talks to an RTX laptop over a link with two paths (Ethernet preferred, Wi-Fi failover); that host serves inference. The transport is HTTP secured with mutual TLS — an unauthenticated inference endpoint on a campus network is an open proxy, so never run one, even briefly for testing.
- **The off-board host is the owner's daily-driver laptop, so it is usually unavailable.** Degraded mode is the normal operating state, not an error path. Two consequences that are invariants, not optimizations: ASR runs on the Pi (off-board Whisper is only an accuracy upgrade when the link is up), and the info-desk role works end to end with no LLM at all — templates are its primary output path, not a fallback.
- **Vectorless RAG** for the college classroom data. No embedding store/vector DB: retrieval is over the structured classroom/location dataset directly (lookup/filter/prompt-stuffing). Do not introduce a vector database to "fix" retrieval without discussing it.
- **Campus data arrives incrementally** and is edited through the admin panel, not in code. The system must be correct at ten rooms and at five hundred: retrieval is coverage-aware, distinguishing "that room isn't in my directory" from "I haven't learned that block yet", and never inventing a room.
- **YOLO detection** for person and object, running on the Pi camera feed. This drives both presence ("someone approached the desk") and the head's gaze target.
- **Motion:** two servos, X (pan) and Y (tilt), moving the head/camera, driven over I2C via a PCA9685 (not Pi GPIO PWM) from a separate 5V supply. Two control paths feed the same servo node — the emotion system and a manual joystick. The joystick is **virtual, in the web app**, so it is network-mediated: it needs a staleness deadman, and a dropped connection must stop the head rather than leave it driving.
- **Emotion layer:** an emotional state modulates head movement (idle motion, gaze, gesture style). It is a movement modifier, not a separate actuator path.
- **Wake word "NEO":** Neo is silent and non-responsive until the wake phrase fires. Any audio/dialogue work must respect this gate — the wake-word detector is upstream of ASR and the LLM, not a filter applied afterwards.

## Source selection (important cross-cutting concern)

Camera, microphone, and speaker each have two possible backends:

- **hardware** — the Pi Camera Module (CSI), a USB mic, and the attached speaker
- **webapp** — the browser device's camera/mic/speaker, via the admin panel

This choice must be runtime-configurable per stream, not a build-time flag or a fork of the node. Node logic downstream of a source must not care which backend is active.

Note that browsers only grant camera and microphone access in a secure context, so the admin panel **must** be served over HTTPS for the webapp backends to work from any device other than the Pi itself. This makes TLS a functional requirement, not just a hardening step.

## Web app = admin panel

The web app manages the whole robot: source selection, head control, vision and audio tuning, dialog monitoring, campus data editing, config, health, and logs. Two things follow:

- It is **split into an always-on light API and an on-demand media bridge**. The camera/mic/speaker WebSocket bridges are the expensive part and must not run when nobody is using them — the Pi has 4 cores and YOLO wants most of them.
- It can move servos and rewrite campus data, so it is behind authentication over HTTPS from the first commit that exposes it.

## Future scope (do not build unless asked)

Full-body locomotion. Currently only the head moves; keep the motion interface general enough that adding a base is not a rewrite, but do not implement drive/navigation now.
