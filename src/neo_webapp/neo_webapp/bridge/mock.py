"""Simulated robot, so the panel is developable with no Pi, no ROS, and no servos.

This is not a toy: it integrates joystick input into a head pose under the same
limits and deadman rules the real servo driver will enforce (plan Phase 3), so the
panel's control path can be exercised and tested for real before hardware exists.
"""

from __future__ import annotations

import asyncio
import math
import time

from .base import Bridge
from .types import (
    Backend,
    HeadState,
    NodeStatus,
    Result,
    RobotState,
    Stream,
)

try:  # optional; falls back to synthetic numbers
    import psutil
except ImportError:  # pragma: no cover - depends on environment
    psutil = None

TICK_HZ = 50.0
MAX_PAN_SPEED_DEG_S = 60.0
MAX_TILT_SPEED_DEG_S = 45.0


class MockBridge(Bridge):
    name = "mock"

    def __init__(self, *, deadman_ms: int = 300, source_switch_ms: int = 250) -> None:
        super().__init__()
        self._state = RobotState(backend=self.name)
        self._state.nodes = [
            NodeStatus(name=n, state="missing")
            for n in (
                "camera_mux",
                "mic_mux",
                "speaker_mux",
                "yolo_node",
                "wake_word",
                "dialog_manager",
                "head_behavior",
                "servo_driver",
            )
        ]
        self._deadman_s = deadman_ms / 1000.0
        self._source_switch_s = source_switch_ms / 1000.0
        self._joy_axes: list[float] = [0.0, 0.0]
        self._joy_stamp = 0.0
        self._task: asyncio.Task | None = None
        self._started = time.monotonic()
        self._frame_times: list[float] = []
        self._mic_bytes = 0
        self._mic_window_start = time.monotonic()
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="mock-bridge-tick")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # -- state -------------------------------------------------------------

    def snapshot(self) -> RobotState:
        self._refresh_system()
        return self._state

    def _refresh_system(self) -> None:
        sysh = self._state.system
        sysh.uptime_s = time.monotonic() - self._started
        if psutil is not None:
            sysh.cpu_percent = psutil.cpu_percent(interval=None)
            vm = psutil.virtual_memory()
            sysh.mem_used_mb = (vm.total - vm.available) / 1e6
            sysh.mem_total_mb = vm.total / 1e6
        else:
            # Synthetic but plausible, so the UI has something to render.
            t = time.monotonic()
            sysh.cpu_percent = 18.0 + 6.0 * math.sin(t / 7.0)
            sysh.mem_used_mb = 900.0 + 40.0 * math.sin(t / 11.0)
            sysh.mem_total_mb = 3900.0

    # -- commands ----------------------------------------------------------

    async def set_source(self, stream: Stream, backend: Backend) -> Result:
        async with self._lock:
            current = getattr(self._state.sources, stream)
            if current == backend:
                return Result(ok=True, message=f"{stream} already on {backend}")
            self._state.sources.transitioning = stream
            try:
                # Stands in for: activate new lifecycle node, wait for ACTIVE,
                # switch the mux, deactivate the old one.
                await asyncio.sleep(self._source_switch_s)
                setattr(self._state.sources, stream, backend)
            finally:
                self._state.sources.transitioning = None
            return Result(ok=True, message=f"{stream} -> {backend}")

    async def set_estop(self, engaged: bool) -> Result:
        self._state.head.estop = engaged
        if engaged:
            self._joy_axes = [0.0, 0.0]
            self._state.dialog_state = "ESTOP"
        elif self._state.dialog_state == "ESTOP":
            self._state.dialog_state = "IDLE"
        return Result(ok=True, message="estop engaged" if engaged else "estop released")

    async def center_head(self) -> Result:
        if self._state.head.estop:
            return Result(ok=False, message="estop engaged")
        self._state.head.pan_deg = 0.0
        self._state.head.tilt_deg = 0.0
        return Result(ok=True, message="centered")

    # -- media in ----------------------------------------------------------

    async def publish_joy(self, axes: list[float], buttons: list[int]) -> None:
        self._joy_axes = [
            _clamp(axes[0] if len(axes) > 0 else 0.0, -1.0, 1.0),
            _clamp(axes[1] if len(axes) > 1 else 0.0, -1.0, 1.0),
        ]
        self._joy_stamp = time.monotonic()

    async def publish_camera_frame(self, jpeg: bytes) -> None:
        now = time.monotonic()
        self._frame_times.append(now)
        cutoff = now - 2.0
        while self._frame_times and self._frame_times[0] < cutoff:
            self._frame_times.pop(0)
        self._state.camera_fps_in = len(self._frame_times) / 2.0

    async def publish_mic_chunk(self, pcm_s16le: bytes) -> None:
        self._mic_bytes += len(pcm_s16le)
        now = time.monotonic()
        window = now - self._mic_window_start
        if window >= 1.0:
            self._state.mic_kbps_in = (self._mic_bytes * 8 / 1000.0) / window
            self._mic_bytes = 0
            self._mic_window_start = now

    # -- simulation --------------------------------------------------------

    async def _run(self) -> None:
        dt = 1.0 / TICK_HZ
        while True:
            self._tick(dt)
            await asyncio.sleep(dt)

    def _tick(self, dt: float) -> None:
        head: HeadState = self._state.head
        stale = (time.monotonic() - self._joy_stamp) > self._deadman_s
        if head.estop or stale:
            # Deadman: a stale or absent joystick means stop, not coast.
            ax, ay = 0.0, 0.0
            head.active_source = "estop" if head.estop else "idle"
        else:
            ax, ay = self._joy_axes
            head.active_source = "manual" if (ax or ay) else "idle"

        pan = head.pan_deg + ax * MAX_PAN_SPEED_DEG_S * dt
        tilt = head.tilt_deg + ay * MAX_TILT_SPEED_DEG_S * dt
        clamped_pan = _clamp(pan, *head.pan_limit_deg)
        clamped_tilt = _clamp(tilt, *head.tilt_limit_deg)
        head.at_limit = (clamped_pan != pan) or (clamped_tilt != tilt)
        head.pan_deg, head.tilt_deg = clamped_pan, clamped_tilt

    # -- test/dev helpers --------------------------------------------------

    def joy_is_stale(self) -> bool:
        return (time.monotonic() - self._joy_stamp) > self._deadman_s

    async def emit_test_tone(self, freq_hz: float = 440.0, ms: int = 400) -> Result:
        """Push a beep down the speaker channel to prove the path end to end."""
        rate = 22050
        n = int(rate * ms / 1000)
        buf = bytearray()
        for i in range(n):
            v = int(12000 * math.sin(2 * math.pi * freq_hz * i / rate))
            buf += int(v).to_bytes(2, "little", signed=True)
        # Chunked, so playback starts before the whole tone is queued.
        for off in range(0, len(buf), 2048):
            await self.emit_audio_out(bytes(buf[off : off + 2048]))
        return Result(ok=True, message=f"{freq_hz:.0f} Hz for {ms} ms")


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))
