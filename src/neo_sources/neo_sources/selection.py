"""Which backend owns each stream, as plain data.

No ROS here, so the rules are testable and the admin panel can reuse them.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

HARDWARE = 0
WEBAPP = 1

STREAM_CAMERA = 1
STREAM_MIC = 2
STREAM_SPEAKER = 3

_STREAM_NAMES = {STREAM_CAMERA: "camera", STREAM_MIC: "mic", STREAM_SPEAKER: "speaker"}
_BACKEND_NAMES = {HARDWARE: "hardware", WEBAPP: "webapp"}


@dataclass(frozen=True)
class Selection:
    """The live backend for each of the three streams."""

    camera: int = HARDWARE
    mic: int = HARDWARE
    speaker: int = HARDWARE

    def with_stream(self, stream: int, backend: int) -> Selection:
        name = _STREAM_NAMES.get(stream)
        if name is None:
            raise ValueError(f"unknown stream {stream}")
        if backend not in _BACKEND_NAMES:
            raise ValueError(f"unknown backend {backend}")
        return replace(self, **{name: backend})

    def describe(self) -> str:
        return ", ".join(
            f"{name}={_BACKEND_NAMES[getattr(self, name)]}"
            for name in ("camera", "mic", "speaker")
        )


def stream_name(stream: int) -> str:
    return _STREAM_NAMES.get(stream, f"stream {stream}")


def backend_name(backend: int) -> str:
    return _BACKEND_NAMES.get(backend, f"backend {backend}")


def validate(stream: int, backend: int) -> tuple[bool, str]:
    """Whether a requested switch is meaningful, and why not if it is not.

    Rejecting an unknown stream or backend here rather than clamping it: a
    caller that sent 7 has a bug, and silently treating it as "hardware" hides
    the bug behind a camera that mysteriously will not switch.
    """
    if stream not in _STREAM_NAMES:
        return False, f"unknown stream {stream} (expected camera=1, mic=2, speaker=3)"
    if backend not in _BACKEND_NAMES:
        return False, f"unknown backend {backend} (expected hardware=0, webapp=1)"
    return True, "ok"
