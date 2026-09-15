"""Simulated robot, so the panel is developable with no Pi, no ROS, and no servos.

This is not a toy, and as of Phase 3 it is not a stand-in either: the head path
here runs the **real** `neo_motion` arbiter and servo driver and the real
`neo_emotion` controller, against a mock servo backend instead of a PCA9685. The
panel therefore exercises production code -- priority order, crossfade, soft
limits, slew rate, deadband, the watchdog, the emotion blend -- and a bug found
here is a bug in the code that will drive the servos.

What is simulated is exactly one thing: the backend that would write pulse
widths to I2C.

Two layers, mirroring the two ROS nodes they will become:

* `HeadArbiter` decides *who* is driving -- estop > manual > gesture > gaze >
  idle -- and expires a source that stops publishing. That expiry is the
  joystick deadman (`head_behavior`).
* `ServoDriver` decides *what actually moves*, trusting nothing upstream
  (`servo_driver`).

Emotion is a modifier, never a source: it shapes the idle drift and the gaze
gain, and has no path of its own to the driver.
"""

from __future__ import annotations

import asyncio
import math
import time

from neo_emotion.motion import EmotionMotion
from neo_emotion.state import EmotionConfig, EmotionController, EmotionEvents
from neo_motion.arbiter import ArbiterConfig, HeadArbiter
from neo_motion.backend import MockServoBackend
from neo_motion.driver import ServoDriver
from neo_motion.types import HeadCommand, HeadLimits, Priority

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

EMOTION_EVERY = 5
"""Ticks between emotion updates: 10 Hz, the rate `emotion_node` will publish at.

The parameters it produces change on human timescales; the motion they shape is
still generated every tick. Recomputing them at 50 Hz would only cost frames the
Pi needs for YOLO.
"""

CAMERA_HFOV_DEG = 60.0
"""Horizontal field of view assumed for the panel's camera: a typical laptop
webcam. Only used to turn "where in the frame" into "which way to face"."""

CAMERA_VFOV_DEG = math.degrees(
    2.0 * math.atan(math.tan(math.radians(CAMERA_HFOV_DEG) / 2.0) * 480.0 / 640.0)
)
"""Vertical, for the 640x480 frames the panel's camera tab sends."""


class MockBridge(Bridge):
    name = "mock"

    def __init__(
        self,
        *,
        deadman_ms: int = 300,
        source_switch_ms: int = 250,
        perception=None,
        idle_motion: bool = True,
    ) -> None:
        """`idle_motion=False` removes the idle source entirely.

        Idle drift is the layer that keeps a still head from looking switched
        off, so it is on for anything a person watches. It is also the reason a
        head is never *exactly* still, which is the opposite of what the deadman
        tests need to assert -- those turn it off to test the safety rule
        without the aesthetic layer moving underneath them.
        """
        super().__init__()
        self._state = RobotState(backend=self.name)
        self.perception = perception
        # No robot voice or robot camera here: the simulated robot's voice is
        # the panel's own loop, and its camera is the browser's.
        self._state.capabilities = ["gestures"] + (["perception"] if perception is not None else [])
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

        # The real motion stack, against a mock backend rather than a PCA9685.
        # The arbiter's source expiry *is* the joystick deadman, which is why
        # the two timeouts are the same number rather than two settings that
        # could drift apart.
        self._limits = HeadLimits()
        self._driver = ServoDriver(limits=self._limits, backend=MockServoBackend())
        self._arbiter = HeadArbiter(ArbiterConfig(source_timeout_s=self._deadman_s))
        self._emotion = EmotionController(EmotionConfig())
        self._emotion_motion = EmotionMotion()
        self._idle_motion = idle_motion

        # The joystick is a rate, integrated to a position target and re-seeded
        # from the *current pose* whenever it takes over, so it cannot resume
        # from a target the head left behind minutes ago.
        self._manual_target: tuple[float, float] | None = None
        self._idle_base = (0.0, 0.0)
        self._ticks = 0
        self._person_seen = False

        # The motion stack runs on a tick clock -- `self._now` advances by `dt`,
        # never by the wall clock. Crossfades and slew limits are then expressed
        # in the same time base as the integration that feeds them, so a test can
        # drive twenty simulated seconds in a millisecond and get the same
        # trajectory the robot would produce in twenty real ones.
        #
        # The joystick deadman is the deliberate exception below: it is a
        # statement about a network, so it stays on the wall clock.
        self._now = 0.0
        self._task: asyncio.Task | None = None
        self._started = time.monotonic()
        self._frame_times: list[float] = []
        self._mic_bytes = 0
        self._mic_window_start = time.monotonic()
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self.perception is not None:
            self.perception.start()
        self._task = asyncio.create_task(self._run(), name="mock-bridge-tick")

    async def stop(self) -> None:
        if self.perception is not None:
            self.perception.stop()
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
        if self.perception is not None:
            self._state.perception = self.perception.view(
                running=self._state.sources.camera == "webapp"
            )
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
        self._driver.set_estop(engaged)
        if engaged:
            self._joy_axes = [0.0, 0.0]
            self._manual_target = None
            self._state.dialog_state = "ESTOP"
        elif self._state.dialog_state == "ESTOP":
            self._state.dialog_state = "IDLE"
        return Result(ok=True, message="estop engaged" if engaged else "estop released")

    async def center_head(self) -> Result:
        if self._state.head.estop:
            return Result(ok=False, message="estop engaged")
        pose = self._driver.center()
        # Every accumulator has to be reset with it. Leaving a stale manual or
        # gaze target behind means the next tick drives straight back out of
        # centre, which looks exactly like the button not working.
        self._manual_target = None
        self._idle_base = (0.0, 0.0)
        self._arbiter.sync_to(pose.pan_rad, pose.tilt_rad)
        self._state.head.pan_deg = 0.0
        self._state.head.tilt_deg = 0.0
        self._state.head.at_limit = False
        return Result(ok=True, message="centered")

    async def trigger_gesture(self, kind: str) -> Result:
        """Play a gesture on the simulated head.

        Through `EmotionMotion`, the same overlay the robot's head_behavior
        mirrors. Here it rides the idle source, so it shows whenever nothing
        higher is driving; on the robot it has its own priority above gaze.
        """
        from neo_emotion.motion import GestureKind

        try:
            gesture = GestureKind((kind or "").strip().lower())
        except ValueError:
            names = ", ".join(g.value for g in GestureKind)
            return Result(ok=False, message=f"unknown gesture {kind!r}; one of {names}")
        if self._state.head.estop:
            return Result(ok=False, message="estop engaged")
        self._emotion_motion.trigger(gesture, self._now)
        self._state.conversation.last_gesture = gesture.value
        return Result(ok=True, message=gesture.value)

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
        if self.perception is not None:
            self.perception.submit_jpeg(jpeg)

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
        """One 50 Hz pass of the real motion stack.

        Order matters and mirrors the two nodes: submit every source that has
        something to say, let the arbiter pick a winner, hand that to the driver,
        then feed the driver's actual pose back so the next crossfade starts from
        where the head *is* rather than from where it was last asked to go.
        """
        self._now += dt
        now = self._now
        head: HeadState = self._state.head
        pose = self._driver.pose

        self._driver.set_estop(head.estop)
        if self._ticks % EMOTION_EVERY == 0:
            self._update_emotion(now)
        self._ticks += 1

        params = self._emotion.state.params

        # --- manual: the virtual joystick ---------------------------------
        # Axes are a *rate*, which is what a spring-centred stick means, so the
        # panel edge integrates them into the position target the wire carries.
        # Freshness is measured against the wall clock, not the tick clock: the
        # thing being detected is a browser that stopped sending over a real
        # network, and "300 ms" has to mean 300 ms of it.
        fresh = not self.joy_is_stale()
        if fresh and any(self._joy_axes):
            if self._manual_target is None:
                self._manual_target = (pose.pan_rad, pose.tilt_rad)
            self._manual_target = self._advance(
                self._manual_target, self._joy_axes[0], self._joy_axes[1], dt
            )
            self._arbiter.submit("manual", HeadCommand(
                pan_rad=self._manual_target[0],
                tilt_rad=self._manual_target[1],
                priority=Priority.MANUAL,
                stamp=now,
            ))
        else:
            self._manual_target = None
            self._arbiter.withdraw("manual")

        # --- gaze: the robot's own idea of where to look ------------------
        target = self.perception.attention() if self.perception is not None else None
        if target is not None and target.engaged and target.confidence > 0.0:
            # A bearing, not a rate. On the robot the camera rides the head, so
            # rate control on image error closes its own loop: turn, and the
            # person slides back towards the middle of the frame. Here the
            # camera is a webcam that does not move when the simulated head
            # does, so that error never shrinks. Integrated as a rate it drove
            # the head from 1 deg to its 90 deg stop in five seconds, measured
            # on real frames, and left it there. Turning to face the person's
            # bearing is what a head looking at them actually does.
            #
            # Emotion shapes how eagerly it turns -- the whole of its influence
            # on gaze, and it still cannot outrank the operator.
            eagerness = max(0.1, min(1.0, params.gaze_gain))
            self._arbiter.submit("gaze", HeadCommand(
                pan_rad=math.radians(target.x * CAMERA_HFOV_DEG / 2.0),
                tilt_rad=math.radians(target.y * CAMERA_VFOV_DEG / 2.0),
                max_speed_rad_s=self._limits.pan.max_speed_rad_s * eagerness,
                priority=Priority.GAZE,
                stamp=now,
            ))
        else:
            # Presence alone never moves the head (CLAUDE.md): someone in view
            # who has not shown a palm is noticed, not followed. Neither does a
            # lock the detector has momentarily lost, whose aim point reads as
            # dead centre -- holding where it was beats swinging to the middle.
            self._arbiter.withdraw("gaze")

        # --- idle: the floor ------------------------------------------------
        # Relative to where the head is resting, not to centre. An absolute idle
        # target would quietly return the head to zero every time the operator
        # let go of the stick, which reads as the robot undoing their aim.
        if self._idle_motion:
            dpan, dtilt = self._emotion_motion.offset_rad(params, now)
            self._arbiter.submit("idle", HeadCommand(
                pan_rad=self._idle_base[0] + dpan,
                tilt_rad=self._idle_base[1] + dtilt,
                priority=Priority.IDLE,
                stamp=now,
            ))

        resolved = self._arbiter.resolve(now)
        self._driver.command(resolved.command)
        pose = self._driver.step(now, dt)
        # Closes the loop: the arbiter's next fade begins from the real pose.
        self._arbiter.sync_to(pose.pan_rad, pose.tilt_rad)

        if resolved.source != "idle":
            self._idle_base = (pose.pan_rad, pose.tilt_rad)

        head.active_source = "estop" if head.estop else resolved.source
        head.pan_deg = math.degrees(pose.pan_rad)
        head.tilt_deg = math.degrees(pose.tilt_rad)
        head.at_limit = self._driver.at_limit

    def _advance(
        self, target: tuple[float, float], ax: float, ay: float, dt: float
    ) -> tuple[float, float]:
        """Integrate a rate into a position target, clamped to the soft limits.

        Clamped here as well as in the driver, because an unclamped accumulator
        run hard into a limit keeps counting: the operator would then have to
        wind several seconds of phantom travel back off before the head moved
        the other way.
        """
        return (
            self._limits.pan.clamp(target[0] + ax * self._limits.pan.max_speed_rad_s * dt),
            self._limits.tilt.clamp(target[1] + ay * self._limits.tilt.max_speed_rad_s * dt),
        )

    def _update_emotion(self, now: float) -> None:
        """Observable events in, a mood and its motion parameters out."""
        present = False
        engaged = False
        if self.perception is not None:
            view = self.perception.view(running=self._state.sources.camera == "webapp")
            present = getattr(view, "person_count", 0) > 0
            engaged = bool(getattr(view, "engaged", False))

        state = self._emotion.update(
            EmotionEvents(
                person_present=present,
                engaged=engaged,
                person_arrived=present and not self._person_seen,
                link_down=not self._state.link.up,
                speaking=self._state.dialog_state == "SPEAKING",
            ),
            now,
        )
        self._person_seen = present
        self._state.emotion.label = state.label.name
        self._state.emotion.intensity = state.intensity

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
