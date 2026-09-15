"""Finding and reading the Pi's camera, without guessing at /dev/video0.

On a Raspberry Pi running Ubuntu, `/dev/video*` is mostly **not cameras**. The
bcm2835 codec and ISP blocks claim a dozen nodes (video10-video31 on this
robot) and they enumerate whether or not anything is plugged in. Opening
`/dev/video0` and hoping is how this goes wrong quietly: a codec node opens
successfully and then never delivers a frame, so the symptom is a perception
pipeline that runs at 0 fps rather than an error anyone can act on.

So the rule here is: a camera is a node that **actually hands back a frame**.
Names are used to order the candidates, never to decide.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

V4L_SYSFS = Path("/sys/class/video4linux")

_NOT_A_CAMERA = ("bcm2835-codec", "bcm2835-isp", "codec", "isp", "rpivid")
"""Kernel blocks that present a V4L2 node but capture nothing. Used only to
sort these last -- a node is still rejected on failing to produce a frame, not
on its name, because a USB camera is free to call itself anything."""


@dataclass(frozen=True)
class CameraDevice:
    index: int
    path: str
    name: str

    @property
    def likely(self) -> bool:
        lowered = self.name.lower()
        return not any(tag in lowered for tag in _NOT_A_CAMERA)


def list_devices() -> list[CameraDevice]:
    """Every V4L2 node, likeliest camera first."""
    found: list[CameraDevice] = []
    if not V4L_SYSFS.exists():
        return found
    for entry in sorted(V4L_SYSFS.iterdir()):
        m = re.fullmatch(r"video(\d+)", entry.name)
        if not m:
            continue
        index = int(m.group(1))
        try:
            name = (entry / "name").read_text().strip()
        except OSError:
            name = ""
        found.append(CameraDevice(index=index, path=f"/dev/video{index}", name=name))
    # Real cameras first, then by index: a USB webcam is usually video0, but
    # "usually" is exactly the assumption this module exists to avoid.
    return sorted(found, key=lambda d: (not d.likely, d.index))


class Camera:
    """A USB (or CSI-as-UVC) camera as an open/read/release stream.

    Wraps OpenCV's VideoCapture and adds the two things a robot needs from it:
    a device that is *verified* to deliver frames, and reopening when it stops.
    """

    def __init__(
        self,
        device: int | str | None = None,
        width: int = 640,
        height: int = 480,
        fps: float = 15.0,
        retry_s: float = 2.0,
    ) -> None:
        self.requested = device
        self.width = width
        self.height = height
        self.fps = fps
        self.retry_s = retry_s
        self._cap = None
        self._opened_path: str | None = None
        self._next_try = 0.0
        self._error: str | None = None

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def device_path(self) -> str | None:
        return self._opened_path

    def _candidates(self) -> list[str | int]:
        if self.requested is not None:
            return [self.requested]
        return [d.path for d in list_devices() if d.likely] or [d.path for d in list_devices()]

    def _try_open(self, target: str | int):
        try:
            import cv2
        except ImportError:
            self._error = "opencv not installed -- pip install -e '.[detector]'"
            return None
        cap = cv2.VideoCapture(target)
        if not cap.isOpened():
            cap.release()
            return None

        # MJPG before the size: at 640x480 a UVC camera that would only manage
        # ~5 fps of raw YUYV over USB 2.0 does 30 in MJPG, and the order
        # matters because the driver picks a format when the size is set.
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        # One frame of latency, not a queue of stale ones: perception wants
        # what the camera sees *now*, and a backlog is worse than a dropped frame.
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:  # noqa: BLE001 - unsupported on some backends, harmless
            pass

        ok, frame = cap.read()
        if not ok or frame is None:
            # This is the codec-node case: opened fine, delivers nothing.
            cap.release()
            return None
        return cap

    def open(self) -> bool:
        if self._cap is not None:
            return True
        now = time.monotonic()
        if now < self._next_try:
            return False
        self._next_try = now + self.retry_s

        candidates = self._candidates()
        if not candidates:
            self._error = "no V4L2 device -- is the USB webcam plugged in?"
            return False
        for target in candidates:
            cap = self._try_open(target)
            if cap is not None:
                self._cap = cap
                self._opened_path = str(target)
                self._error = None
                log.info("camera open on %s at %dx%d", target, self.width, self.height)
                return True
        self._error = (
            f"none of {', '.join(str(c) for c in candidates)} delivered a frame. "
            f"A Pi's /dev/video* is mostly codec nodes; a USB webcam adds its own."
        )
        return False

    def read(self):
        """A BGR frame, or None if the camera is not (yet) delivering one."""
        if not self.open():
            return None
        ok, frame = self._cap.read()
        if not ok or frame is None:
            # Unplugged mid-stream, or the driver wedged. Drop it so the next
            # call re-runs discovery instead of polling a dead handle.
            self.release()
            self._error = "capture failed -- device unplugged?"
            return None
        return frame

    def release(self) -> None:
        cap, self._cap = self._cap, None
        self._opened_path = None
        if cap is not None:
            try:
                cap.release()
            except Exception:  # noqa: BLE001 - releasing a dead handle is not an error
                pass

    def available(self) -> tuple[bool, str]:
        try:
            import cv2  # noqa: F401
        except ImportError:
            return False, "opencv not installed -- pip install -e '.[detector]'"
        if not list_devices():
            return False, "no V4L2 device -- is the USB webcam plugged in?"
        return True, "ok"
