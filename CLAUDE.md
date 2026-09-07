# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

The repository is currently empty apart from `README.md` — there is no source code, build system, or test suite yet. Everything below is the intended design, provided by the project owner. When scaffolding, prefer creating the structure described here over inventing a new one, and update this file as real commands and layout appear.

## Project: Neo

Neo is a receptionist robot for a college campus, running on a Raspberry Pi 4 (4GB). Two roles:

1. **Info desk** — answers questions about the campus (e.g. "where is classroom CS-204?", "how do I get to B block?") from college classroom/location data.
2. **General AI assistant** — conversational answers outside the info-desk domain.

It is head-only for now (pan/tilt), wake-word gated, and has a web app that can stand in for its physical I/O during development.

## Architecture (intended)

- **ROS 2 Jazzy** is the integration layer. Perception, LLM, motion, and audio are separate nodes communicating over topics/services, so any of them can be swapped for a simulated/web-app source without the others knowing.
- **LLM: Qwen 2.5 3B, off-board.** The model does not run on the Pi. The Pi talks to an RTX laptop over a direct point-to-point link; that host serves inference. Assume the link can drop — the Pi side needs timeouts and a degraded mode.
- **Vectorless RAG** for the college classroom data. No embedding store/vector DB: retrieval is over the structured classroom/location dataset directly (lookup/filter/prompt-stuffing). Do not introduce a vector database to "fix" retrieval without discussing it.
- **YOLO detection** for person and object, running on the Pi camera feed. This drives both presence ("someone approached the desk") and the head's gaze target.
- **Motion:** two servos, X (pan) and Y (tilt), moving the head/camera. Two control paths feed the same servo node — the emotion system and a manual joystick controller.
- **Emotion layer:** an emotional state modulates head movement (idle motion, gaze, gesture style). It is a movement modifier, not a separate actuator path.
- **Wake word "NEO":** Neo is silent and non-responsive until the wake phrase fires. Any audio/dialogue work must respect this gate — the wake-word detector is upstream of ASR and the LLM, not a filter applied afterwards.

## Source selection (important cross-cutting concern)

Camera, microphone, and speaker each have two possible backends:

- **hardware** — the Pi's attached camera/mic/speaker and servos
- **webapp** — the browser acts as a pseudo camera/mic/speaker for development and testing

This choice must be runtime-configurable per stream, not a build-time flag or a fork of the node. Node logic downstream of a source must not care which backend is active.

## Future scope (do not build unless asked)

Full-body locomotion. Currently only the head moves; keep the motion interface general enough that adding a base is not a rewrite, but do not implement drive/navigation now.
