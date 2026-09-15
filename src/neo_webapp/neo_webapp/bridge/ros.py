"""ROS 2 backend: the admin panel as a client of the robot's graph.

Deliberately the only file in the package that imports rclpy, so the panel stays
runnable on a machine with no ROS installed. The rclpy executor runs on its own
thread; every hand-off to the API goes through `loop.call_soon_threadsafe`,
because FastAPI's handlers all live on the event loop and read `self._state`
there. No subscription callback touches that state directly.

What "control" means over ROS, and where each lever lands:

* **Sources.** /sources/set on the source manager, which is also the mux: a
  browser's camera and mic reach the graph only while their stream is on webapp.
* **Head.** /head/center on head_behavior, /head/estop on servo_driver, /joy for
  the joystick. The arbiter source actually winning is read off /head/command's
  priority, so the panel shows what the head is doing, not what was requested.
* **Voice.** The robot's own conversation -- wake score, what it heard, what it
  said -- and the operator's ways into it: open the listening window, ask a
  typed question, make it say something, stop it talking.
* **Vision.** `self.perception` has the shape of the in-process PerceptionLink
  the Vision routes already use, answered over /perception/identify and
  /perception/release. The robot camera's picture is fetched on demand only.
* **Emotion.** /emotion/gesture into head_behavior at GESTURE priority, below the
  joystick, so a nod button can never fight an operator.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from pathlib import Path
from typing import Any

from .base import Bridge
from .types import (
    Backend,
    IdentifyView,
    NodeStatus,
    ObjectGuessView,
    Result,
    RobotState,
    Stream,
    TrackView,
)

try:
    import psutil
except ImportError:  # pragma: no cover - optional dependency
    psutil = None

# neo_msgs constants, mirrored as plain data so this module needs no neo_msgs
# import until `start()` actually runs (see probe()).
_STREAM_TO_MSG = {"camera": 1, "mic": 2, "speaker": 3}
_BACKEND_TO_MSG = {"hardware": 0, "webapp": 1}
_BACKEND_FROM_MSG = {0: "hardware", 1: "webapp"}
_STREAM_FROM_MSG = {0: None, 1: "camera", 2: "mic", 3: "speaker"}
_PATH_FROM_MSG = {0: "none", 1: "eth", 2: "wifi"}
_EMOTION_LABEL_NAMES = ("NEUTRAL", "ATTENTIVE", "HAPPY", "CURIOUS", "CONFUSED", "SLEEPY")
_DIALOG_NAMES = ("IDLE", "LISTENING", "THINKING", "SPEAKING")
_PRIORITY_NAMES = {10: "idle", 30: "gaze", 50: "gesture", 70: "manual", 90: "estop"}
_ENGAGEMENT_NAMES = ("scanning", "engaging", "engaged", "suspended")
_GESTURE_NAMES = ("none", "raised_hand", "open_palm", "wave")
_REPLY_SOURCE_NAMES = ("kb", "llm", "kb_polished", "fallback")

GESTURES = ("nod", "shake", "tilt", "scan")
"""What /emotion/gesture accepts: the keys of head_behavior's GESTURE_SHAPES."""

MAX_ROBOT_TEXT = 600

SERVICE_TIMEOUT_S = 3.0
IDENTIFY_TIMEOUT_S = 8.0
"""Longer than perception's own identify timeout, so its answer -- including an
honest "timed out" -- arrives before this gives up on the round trip."""

WAKE_FRESH_S = 2.0
PERCEPTION_FRESH_S = 1.5
OBJECTS_FRESH_S = 3.0
CAMERA_IDLE_S = 5.0
"""How long the robot-camera subscription outlives the last picture request.

Raw 640x480 BGR at 15 fps is ~13 MB/s through DDS, in a panel process on the
same four cores YOLO wants. It is paid only while somebody is looking (plan
0.3.4), and the subscription is dropped a few seconds after they stop.
"""

DEFAULT_FRAME_SIZE = (640, 480)
"""camera_hw's default resolution. Detection boxes arrive in pixels; this
normalises them until a real frame has told the bridge the actual size."""

_FALLBACK_NODES = (
    "source_manager", "camera_hw", "mic_hw", "speaker_hw", "servo_driver",
    "head_behavior", "perception", "wake_word", "asr_router", "tts", "link",
    "dialog", "emotion",
)


class RosUnavailable(RuntimeError):
    """Raised when the ROS backend is requested but its dependencies are absent."""


def probe() -> tuple[bool, str]:
    """Report whether the ROS backend can run here, and why not if it can't."""
    try:
        import rclpy  # noqa: F401
    except ImportError:
        return False, "rclpy not installed"
    try:
        import neo_msgs.msg  # noqa: F401
    except ImportError:
        return False, "neo_msgs not built (Phase 1)"
    return True, "ok"


def _expected_nodes() -> tuple[str, ...]:
    """The node registry neo_bringup launches from, so the System tab's list
    cannot drift from what bringup actually starts."""
    try:
        import yaml
        from ament_index_python.packages import get_package_share_directory

        path = Path(get_package_share_directory("neo_bringup")) / "config" / "nodes.yaml"
        return tuple(yaml.safe_load(path.read_text(encoding="utf-8"))["nodes"])
    except Exception:  # noqa: BLE001 - a missing registry only costs the node list
        return _FALLBACK_NODES


def _read_temp_c() -> float | None:
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def _encode_jpeg(msg: Any) -> bytes | None:
    """A sensor_msgs/Image as JPEG bytes, or None if it cannot be encoded."""
    if msg.encoding == "jpeg":
        return bytes(msg.data)
    channels = {"bgr8": 3, "rgb8": 3, "mono8": 1}.get(msg.encoding)
    if channels is None or not msg.height or not msg.width:
        return None
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    raw = np.frombuffer(bytes(msg.data), dtype=np.uint8)
    step = int(msg.step) or int(msg.width) * channels
    if raw.size < step * msg.height:
        return None
    rows = raw[: step * msg.height].reshape(msg.height, step)[:, : msg.width * channels]
    frame = rows.reshape(msg.height, msg.width, channels) if channels > 1 else rows
    if msg.encoding == "rgb8":
        frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
    ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
    return encoded.tobytes() if ok else None


class _RosPerception:
    """The `bridge.perception` the Vision routes expect, answered over ROS.

    On the simulated robot, perception runs inside the panel and the routes call
    it directly. On the real robot it is its own node; this keeps the routes
    unchanged by giving them the same four calls.
    """

    def __init__(self, bridge: RosBridge) -> None:
        self._bridge = bridge
        self._requested = 0
        self._identify = IdentifyView()

    def start(self) -> None:
        """Nothing to start: perception is a node, not a thread in this process."""

    def stop(self) -> None:
        """Nothing to stop, for the same reason."""

    def release(self, reason: str = "panel override") -> None:
        self._bridge._spawn(self._bridge._trigger("_release_client"))

    def reset(self) -> None:
        # The node has no reset service. Dropping the lock is the part of a
        # reset that matters remotely; tracks re-form on their own within a
        # few frames.
        self.release("panel reset")

    def request_identify(self) -> int:
        self._requested += 1
        target = self._requested
        self._identify = IdentifyView(
            pending=True, seq=self._identify.seq, error="", guesses=list(self._identify.guesses)
        )
        self._bridge._spawn(self._run_identify(target))
        return target

    async def _run_identify(self, target: int) -> None:
        from std_srvs.srv import Trigger

        error = ""
        guesses: list[ObjectGuessView] = []
        try:
            response = await self._bridge._call(
                self._bridge._identify_client, Trigger.Request(), timeout_s=IDENTIFY_TIMEOUT_S
            )
        except RosUnavailable:
            error = "the perception node is not running"
        except asyncio.TimeoutError:
            error = "object identification timed out"
        else:
            if not response.success:
                error = response.message or "identification failed"
            else:
                guesses = self._bridge._objects_for(response.message or "")
        self._identify = IdentifyView(pending=False, seq=target, error=error, guesses=guesses)

    @property
    def identify(self) -> IdentifyView:
        return self._identify

    def view(self, running: bool = True):
        view = self._bridge._state.perception
        view.identify = self._identify
        return view


class RosBridge(Bridge):
    name = "ros"

    owns_recognition = True
    """Browser mic audio goes into the graph, where the robot's own wake word and
    recogniser listen to it. The panel running a second Vosk over the same audio
    would spend a Pi core transcribing what the robot is already transcribing."""

    def __init__(self, *, node_name: str = "neo_admin_panel") -> None:
        super().__init__()
        ok, why = probe()
        if not ok:
            raise RosUnavailable(why)
        self._node_name = node_name
        self._state = RobotState(
            backend=self.name,
            capabilities=["robot_voice", "robot_camera", "gestures", "perception"],
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._node: Any = None
        self._executor: Any = None
        self._tasks: set[asyncio.Task] = set()

        self.perception = _RosPerception(self)
        self._expected_nodes = _expected_nodes()
        self._started = time.monotonic()
        self._temp_checked = 0.0
        self._mic_seq = 0

        self._wake_stamp = 0.0
        self._wake_fired_at: float | None = None
        self._attention_stamp = 0.0
        self._objects: list[tuple[str, float, float, float, float, float]] = []
        self._objects_stamp = 0.0

        self._frame_size = DEFAULT_FRAME_SIZE
        self._frame_lock = threading.Lock()
        self._frame_msg: Any = None
        self._frame_stamp = 0.0
        self._camera_sub: Any = None
        self._camera_wanted_until = 0.0

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        import rclpy
        from rclpy.executors import MultiThreadedExecutor
        from rclpy.node import Node

        self._loop = asyncio.get_running_loop()
        rclpy.init(args=None)
        self._node = Node(self._node_name)
        self._executor = MultiThreadedExecutor()
        self._executor.add_node(self._node)
        self._declare_io()
        self._load_head_limits()
        self._node.create_timer(2.0, self._refresh_nodes)
        self._node.create_timer(0.5, self._manage_camera)
        self._thread = threading.Thread(
            target=self._executor.spin, name="rclpy-spin", daemon=True
        )
        self._thread.start()

    async def stop(self) -> None:
        import rclpy

        for task in list(self._tasks):
            task.cancel()
        if self._executor is not None:
            self._executor.shutdown()
        if self._node is not None:
            self._node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _load_head_limits(self) -> None:
        """Show the limits the servo driver actually enforces, not the defaults.

        The panel imports neo_motion anyway, and its config is what the driver
        on this same machine loaded -- so the Head tab's range cannot disagree
        with the head's.
        """
        try:
            from neo_motion.config import MotionConfig

            limits = MotionConfig.load().head_limits()
        except Exception:  # noqa: BLE001 - an unreadable config only costs the labels
            return
        head = self._state.head
        head.pan_limit_deg = (math.degrees(limits.pan.min_rad), math.degrees(limits.pan.max_rad))
        head.tilt_limit_deg = (math.degrees(limits.tilt.min_rad), math.degrees(limits.tilt.max_rad))

    # -- plumbing ------------------------------------------------------------

    def _post(self, fn, *args) -> None:
        """Run `fn(*args)` on the event loop. Safe from the rclpy thread."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(fn, *args)

    def _spawn(self, coro) -> None:
        """Start a coroutine from the event loop and keep a reference to it."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _stamp(self):
        return self._node.get_clock().now().to_msg()

    def _declare_io(self) -> None:
        """Publishers, subscriptions and clients.

          subscribe  /dialog/state /dialog/transcript /dialog/reply /dialog/speaking
                     /wake/score /wake/event
                     /link/health /sources/state (latched)
                     /head/state /head/command /emotion/state /emotion/gesture
                     /perception/attention /perception/gestures
                     /perception/detections /perception/objects
                     /audio/webapp/out
                     /camera/image_raw        -- only while a picture is wanted
          publish    /joy /camera/webapp/image_raw /audio/webapp/in
                     /wake/event /dialog/transcript /dialog/reply /dialog/cancel
                     /emotion/gesture
          client     /sources/set /head/center /head/estop
                     /perception/release /perception/identify
        """
        from neo_msgs.msg import (
            AttentionTarget,
            AudioChunk,
            DialogState,
            EmotionState,
            GestureEvent,
            HeadCommand,
            LinkHealth,
            Reply,
            SourceState,
            Transcript,
            WakeEvent,
        )
        from neo_msgs.srv import SetSource
        from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
        from sensor_msgs.msg import Image, Joy, JointState
        from std_msgs.msg import Bool, Empty, Float32, String
        from std_srvs.srv import SetBool, Trigger

        n = self._node
        latest = QoSProfile(
            depth=1, reliability=ReliabilityPolicy.BEST_EFFORT, history=HistoryPolicy.KEEP_LAST
        )
        audio_in = QoSProfile(
            depth=10, reliability=ReliabilityPolicy.BEST_EFFORT, history=HistoryPolicy.KEEP_LAST
        )
        # Speech bursts a sentence at a time, far faster than real time; a
        # depth-1 queue here would keep one chunk in every burst and drop the rest.
        speech = QoSProfile(
            depth=64, reliability=ReliabilityPolicy.RELIABLE, history=HistoryPolicy.KEEP_LAST
        )
        latched = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            history=HistoryPolicy.KEEP_LAST,
        )

        n.create_subscription(DialogState, "/dialog/state", self._on_dialog_state, 10)
        n.create_subscription(Transcript, "/dialog/transcript", self._on_transcript, 10)
        n.create_subscription(Reply, "/dialog/reply", self._on_reply, 10)
        n.create_subscription(Bool, "/dialog/speaking", self._on_speaking, 10)
        n.create_subscription(Float32, "/wake/score", self._on_wake_score, latest)
        n.create_subscription(WakeEvent, "/wake/event", self._on_wake_event, 10)
        n.create_subscription(LinkHealth, "/link/health", self._on_link_health, 10)
        n.create_subscription(SourceState, "/sources/state", self._on_source_state, latched)
        n.create_subscription(JointState, "/head/state", self._on_head_state, latest)
        n.create_subscription(HeadCommand, "/head/command", self._on_head_command, latest)
        n.create_subscription(EmotionState, "/emotion/state", self._on_emotion_state, 10)
        n.create_subscription(String, "/emotion/gesture", self._on_emotion_gesture, 10)
        n.create_subscription(AttentionTarget, "/perception/attention", self._on_attention, 10)
        n.create_subscription(GestureEvent, "/perception/gestures", self._on_perception_gesture, 10)
        n.create_subscription(AudioChunk, "/audio/webapp/out", self._on_audio_out, speech)
        try:
            from vision_msgs.msg import Detection2DArray
        except ImportError:  # detections are cosmetic here; the rest still works
            pass
        else:
            n.create_subscription(Detection2DArray, "/perception/detections", self._on_detections, 10)
            n.create_subscription(Detection2DArray, "/perception/objects", self._on_objects, 10)

        self._joy_pub = n.create_publisher(Joy, "/joy", latest)
        self._camera_pub = n.create_publisher(Image, "/camera/webapp/image_raw", latest)
        self._mic_pub = n.create_publisher(AudioChunk, "/audio/webapp/in", audio_in)
        self._wake_pub = n.create_publisher(WakeEvent, "/wake/event", 10)
        self._transcript_pub = n.create_publisher(Transcript, "/dialog/transcript", 10)
        self._reply_pub = n.create_publisher(Reply, "/dialog/reply", 10)
        self._cancel_pub = n.create_publisher(Empty, "/dialog/cancel", 10)
        self._gesture_pub = n.create_publisher(String, "/emotion/gesture", 10)

        self._set_source_client = n.create_client(SetSource, "/sources/set")
        self._center_client = n.create_client(Trigger, "/head/center")
        self._estop_client = n.create_client(SetBool, "/head/estop")
        self._release_client = n.create_client(Trigger, "/perception/release")
        self._identify_client = n.create_client(Trigger, "/perception/identify")

        self._state.nodes = [NodeStatus(name=self._node_name, state="active")]

    # -- state -------------------------------------------------------------

    def snapshot(self) -> RobotState:
        now = time.monotonic()
        self._refresh_system(now)

        wake = self._state.wake
        wake.available = (now - self._wake_stamp) < WAKE_FRESH_S
        if not wake.available:
            wake.score = 0.0
        wake.last_fired_age_s = None if self._wake_fired_at is None else now - self._wake_fired_at

        perception = self._state.perception
        fresh = (now - self._attention_stamp) < PERCEPTION_FRESH_S
        perception.available = fresh
        perception.running = fresh
        perception.detector = "perception node" if fresh else "none"
        if not fresh:
            perception.tracks = []
            perception.person_count = 0
        perception.identify = self.perception.identify
        return self._state

    def _refresh_system(self, now: float) -> None:
        sysh = self._state.system
        sysh.uptime_s = now - self._started
        if psutil is not None:
            sysh.cpu_percent = psutil.cpu_percent(interval=None)
            vm = psutil.virtual_memory()
            sysh.mem_used_mb = (vm.total - vm.available) / 1e6
            sysh.mem_total_mb = vm.total / 1e6
        if now - self._temp_checked >= 5.0:
            # A single sysfs read, every five seconds: cheap enough to sit in the
            # snapshot, unlike the status files the 1 Hz task handles.
            self._temp_checked = now
            sysh.temp_c = _read_temp_c()

    # -- subscription callbacks (rclpy thread) -------------------------------
    #
    # Each extracts plain values and hops to the event loop before touching
    # self._state -- FastAPI's handlers, which read it, all live there.

    def _on_dialog_state(self, msg) -> None:
        self._post(self._set_dialog_state, int(msg.state), bool(msg.degraded), bool(msg.estop))

    def _set_dialog_state(self, state: int, degraded: bool, estop: bool) -> None:
        if estop or self._state.head.estop:
            value = "ESTOP"
        elif state == 0 and degraded:
            # No model host is the robot's *normal* state (CLAUDE.md), so it
            # only takes the badge when nothing else is happening. Showing it
            # over LISTENING or SPEAKING would hide the conversation behind a
            # fact that is almost always true.
            value = "DEGRADED"
        else:
            value = _DIALOG_NAMES[state] if 0 <= state < len(_DIALOG_NAMES) else "IDLE"
        self._state.dialog_state = value

    def _on_transcript(self, msg) -> None:
        self._post(self._set_transcript, str(msg.text), bool(msg.is_final))

    def _set_transcript(self, text: str, is_final: bool) -> None:
        convo = self._state.conversation
        if is_final:
            convo.heard = text
            convo.partial = ""
        else:
            convo.partial = text

    def _on_reply(self, msg) -> None:
        source = _REPLY_SOURCE_NAMES[msg.source] if msg.source < len(_REPLY_SOURCE_NAMES) else "?"
        self._post(self._set_reply, str(msg.text), source, float(msg.latency_ms))

    def _set_reply(self, text: str, source: str, latency_ms: float) -> None:
        convo = self._state.conversation
        convo.reply = text
        convo.reply_source = source
        convo.reply_latency_ms = latency_ms
        convo.turns += 1

    def _on_speaking(self, msg) -> None:
        self._post(setattr, self._state.conversation, "speaking", bool(msg.data))

    def _on_wake_score(self, msg) -> None:
        self._post(self._set_wake_score, float(msg.data))

    def _set_wake_score(self, score: float) -> None:
        self._wake_stamp = time.monotonic()
        self._state.wake.score = score

    def _on_wake_event(self, msg) -> None:
        self._post(
            self._set_wake_event,
            float(msg.score),
            float(msg.threshold_applied),
            bool(msg.person_present),
        )

    def _set_wake_event(self, score: float, threshold: float, person: bool) -> None:
        wake = self._state.wake
        wake.fired += 1
        wake.last_score = score
        wake.last_threshold = threshold
        wake.last_person_present = person
        self._wake_fired_at = time.monotonic()

    def _on_link_health(self, msg) -> None:
        self._post(
            self._set_link_health,
            bool(msg.up),
            int(msg.active_path),
            float(msg.rtt_ms),
            int(msg.consecutive_failures),
        )

    def _set_link_health(self, up: bool, path: int, rtt_ms: float, failures: int) -> None:
        link = self._state.link
        link.up = up
        link.active_path = _PATH_FROM_MSG.get(path, "none")
        link.rtt_ms = rtt_ms if up else None
        link.consecutive_failures = failures

    def _on_head_state(self, msg) -> None:
        if len(msg.position) < 2:
            return
        self._post(self._set_head_state, float(msg.position[0]), float(msg.position[1]))

    def _set_head_state(self, pan_rad: float, tilt_rad: float) -> None:
        head = self._state.head
        head.pan_deg = math.degrees(pan_rad)
        head.tilt_deg = math.degrees(tilt_rad)
        margin = 0.5
        head.at_limit = (
            head.pan_deg <= head.pan_limit_deg[0] + margin
            or head.pan_deg >= head.pan_limit_deg[1] - margin
            or head.tilt_deg <= head.tilt_limit_deg[0] + margin
            or head.tilt_deg >= head.tilt_limit_deg[1] - margin
        )

    def _on_head_command(self, msg) -> None:
        self._post(self._set_active_source, int(msg.priority))

    def _set_active_source(self, priority: int) -> None:
        self._state.head.active_source = _PRIORITY_NAMES.get(priority, f"priority {priority}")

    def _on_emotion_state(self, msg) -> None:
        self._post(self._set_emotion_state, int(msg.label), float(msg.intensity))

    def _set_emotion_state(self, label: int, intensity: float) -> None:
        if 0 <= label < len(_EMOTION_LABEL_NAMES):
            self._state.emotion.label = _EMOTION_LABEL_NAMES[label]
        self._state.emotion.intensity = intensity

    def _on_emotion_gesture(self, msg) -> None:
        self._post(setattr, self._state.conversation, "last_gesture", str(msg.data))

    def _on_source_state(self, msg) -> None:
        self._post(
            self._set_source_state,
            int(msg.camera),
            int(msg.mic),
            int(msg.speaker),
            int(msg.transitioning),
        )

    def _set_source_state(self, camera: int, mic: int, speaker: int, transitioning: int) -> None:
        sources = self._state.sources
        sources.camera = _BACKEND_FROM_MSG.get(camera, "hardware")
        sources.mic = _BACKEND_FROM_MSG.get(mic, "hardware")
        sources.speaker = _BACKEND_FROM_MSG.get(speaker, "hardware")
        sources.transitioning = _STREAM_FROM_MSG.get(transitioning)

    def _on_attention(self, msg) -> None:
        no_track = getattr(msg, "NO_TRACK", -1)
        self._post(
            self._set_attention,
            int(msg.state),
            bool(msg.engaged),
            None if msg.track_id == no_track else int(msg.track_id),
            float(msg.x),
            float(msg.y),
            bool(msg.person_present),
        )

    def _set_attention(
        self, state: int, engaged: bool, track_id: int | None, x: float, y: float, present: bool
    ) -> None:
        self._attention_stamp = time.monotonic()
        p = self._state.perception
        p.state = _ENGAGEMENT_NAMES[state] if 0 <= state < len(_ENGAGEMENT_NAMES) else "scanning"
        p.engaged = engaged
        p.target_id = track_id
        p.aim_x = x
        p.aim_y = y
        if not present:
            p.person_count = 0
            p.tracks = []
        for track in p.tracks:
            track.engaged = engaged and track.track_id == track_id

    def _on_perception_gesture(self, msg) -> None:
        if 0 < msg.kind < len(_GESTURE_NAMES):
            self._post(setattr, self._state.perception, "last_gesture", _GESTURE_NAMES[msg.kind])

    def _on_detections(self, msg) -> None:
        width, height = self._frame_size
        tracks = []
        for index, det in enumerate(msg.detections):
            cx, cy = det.bbox.center.position.x, det.bbox.center.position.y
            half_w, half_h = det.bbox.size_x / 2.0, det.bbox.size_y / 2.0
            try:
                track_id = int(det.id)
            except (TypeError, ValueError):
                track_id = index
            tracks.append(
                TrackView(
                    track_id=track_id,
                    x1=max(0.0, (cx - half_w) / width),
                    y1=max(0.0, (cy - half_h) / height),
                    x2=min(1.0, (cx + half_w) / width),
                    y2=min(1.0, (cy + half_h) / height),
                    confirmed=True,
                )
            )
        self._post(self._set_tracks, tracks)

    def _set_tracks(self, tracks: list[TrackView]) -> None:
        p = self._state.perception
        for track in tracks:
            track.engaged = p.engaged and track.track_id == p.target_id
        p.tracks = tracks
        p.person_count = len(tracks)

    def _on_objects(self, msg) -> None:
        objects = []
        for det in msg.detections:
            if not det.results:
                continue
            hyp = det.results[0].hypothesis
            objects.append(
                (
                    str(hyp.class_id),
                    float(hyp.score),
                    det.bbox.center.position.x,
                    det.bbox.center.position.y,
                    det.bbox.size_x,
                    det.bbox.size_y,
                )
            )
        self._post(self._set_objects, objects)

    def _set_objects(self, objects) -> None:
        self._objects = objects
        self._objects_stamp = time.monotonic()

    def _objects_for(self, best: str) -> list[ObjectGuessView]:
        """The ranked guesses for an identification whose best label is `best`.

        The service answers with the label alone; the boxes come from the
        /perception/objects array it publishes just before answering. If that
        array is stale -- or never arrived -- the label still stands on its own.
        """
        width, height = self._frame_size
        guesses: list[ObjectGuessView] = []
        if time.monotonic() - self._objects_stamp < OBJECTS_FRESH_S:
            for label, score, cx, cy, sw, sh in self._objects:
                guesses.append(
                    ObjectGuessView(
                        label=label,
                        confidence=score,
                        prominence=0.0,
                        x1=max(0.0, (cx - sw / 2) / width),
                        y1=max(0.0, (cy - sh / 2) / height),
                        x2=min(1.0, (cx + sw / 2) / width),
                        y2=min(1.0, (cy + sh / 2) / height),
                    )
                )
        if best and (not guesses or guesses[0].label != best):
            guesses.insert(0, ObjectGuessView(best, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
        return guesses

    def _on_audio_out(self, msg) -> None:
        # Speech, so the back-pressured push rather than the drop-on-full path
        # -- see Bridge.push_audio_out on why emit_audio_out would truncate it.
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self.push_audio_out(bytes(msg.data)), self._loop)

    def _refresh_nodes(self) -> None:
        try:
            live = set(self._node.get_node_names())
        except Exception:  # noqa: BLE001 - the graph API can race shutdown
            return
        statuses = [NodeStatus(name=self._node_name, state="active")] + [
            NodeStatus(name=name, state="active" if name in live else "missing")
            for name in self._expected_nodes
        ]
        self._post(setattr, self._state, "nodes", statuses)

    # -- the robot camera, on demand -----------------------------------------

    def _on_camera_frame(self, msg) -> None:
        with self._frame_lock:
            self._frame_msg = msg
            self._frame_stamp = time.monotonic()
        if msg.width and msg.height:
            self._frame_size = (int(msg.width), int(msg.height))

    def _manage_camera(self) -> None:
        """Create or drop the camera subscription, on the rclpy thread.

        Entities are only ever created and destroyed here, never from the event
        loop, so the executor is not racing a subscription being torn down
        underneath one of its callbacks.
        """
        from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
        from sensor_msgs.msg import Image

        wanted = time.monotonic() < self._camera_wanted_until
        if wanted and self._camera_sub is None:
            qos = QoSProfile(
                depth=1, reliability=ReliabilityPolicy.BEST_EFFORT, history=HistoryPolicy.KEEP_LAST
            )
            self._camera_sub = self._node.create_subscription(
                Image, "/camera/image_raw", self._on_camera_frame, qos
            )
        elif not wanted and self._camera_sub is not None:
            self._node.destroy_subscription(self._camera_sub)
            self._camera_sub = None
            with self._frame_lock:
                self._frame_msg = None

    async def camera_snapshot(self, max_age_s: float = 2.0) -> bytes | None:
        """The robot camera's latest picture as JPEG, or None while it warms up.

        The first request only arms the subscription; the picture follows on
        the next one, a moment later. That is the price of not streaming raw
        frames into the panel when nobody is looking.
        """
        self._camera_wanted_until = time.monotonic() + CAMERA_IDLE_S
        with self._frame_lock:
            msg, stamp = self._frame_msg, self._frame_stamp
        if msg is None or (time.monotonic() - stamp) > max_age_s:
            return None
        return await asyncio.to_thread(_encode_jpeg, msg)

    # -- service call bridging (asyncio thread) ------------------------------

    async def _call(self, client, request, timeout_s: float = SERVICE_TIMEOUT_S):
        if not client.service_is_ready():
            raise RosUnavailable(f"{client.srv_name} has no server")
        rclpy_future = client.call_async(request)
        asyncio_future: asyncio.Future = self._loop.create_future()

        def _done(f) -> None:
            if asyncio_future.cancelled():
                return
            try:
                result = f.result()
            except Exception as exc:  # noqa: BLE001 - surfaced to the caller below
                self._loop.call_soon_threadsafe(asyncio_future.set_exception, exc)
            else:
                self._loop.call_soon_threadsafe(asyncio_future.set_result, result)

        rclpy_future.add_done_callback(_done)
        return await asyncio.wait_for(asyncio_future, timeout_s)

    async def _trigger(self, client_attr: str) -> Result:
        from std_srvs.srv import Trigger

        try:
            response = await self._call(getattr(self, client_attr), Trigger.Request())
        except (RosUnavailable, asyncio.TimeoutError) as exc:
            return Result(ok=False, message=str(exc) or "timed out")
        return Result(ok=bool(response.success), message=response.message)

    # -- commands ------------------------------------------------------------

    async def set_source(self, stream: Stream, backend: Backend) -> Result:
        from neo_msgs.srv import SetSource

        req = SetSource.Request()
        req.stream = _STREAM_TO_MSG[stream]
        req.backend = _BACKEND_TO_MSG[backend]
        try:
            resp = await self._call(self._set_source_client, req)
        except (RosUnavailable, asyncio.TimeoutError) as exc:
            return Result(ok=False, message=str(exc) or "timed out")
        return Result(ok=resp.success, message=resp.message)

    async def set_estop(self, engaged: bool) -> Result:
        from std_msgs.msg import Empty
        from std_srvs.srv import SetBool

        if engaged:
            # Silence and a neutral stick first, before the round trip: neither
            # needs the servo driver to be up, and both are part of what an
            # e-stop means to the person standing in front of the robot.
            await self.publish_joy([0.0, 0.0], [])
            self._cancel_pub.publish(Empty())

        req = SetBool.Request()
        req.data = engaged
        try:
            resp = await self._call(self._estop_client, req)
        except (RosUnavailable, asyncio.TimeoutError) as exc:
            # Not marked engaged: the servos have not confirmed anything, and a
            # badge reading ESTOP over a head that is still driving is the one
            # lie this panel must never tell.
            return Result(ok=False, message=f"servo driver did not confirm: {exc or 'timed out'}")
        if resp.success:
            self._state.head.estop = engaged
            self._state.dialog_state = "ESTOP" if engaged else "IDLE"
        return Result(ok=resp.success, message=resp.message)

    async def center_head(self) -> Result:
        if self._state.head.estop:
            return Result(ok=False, message="estop engaged")
        return await self._trigger("_center_client")

    async def robot_wake(self) -> Result:
        """Open the robot's listening window, as if it had heard "NEO"."""
        from neo_msgs.msg import WakeEvent

        if self._state.head.estop:
            return Result(ok=False, message="estop engaged")
        msg = WakeEvent()
        msg.header.stamp = self._stamp()
        msg.score = 1.0
        # 0.0: nothing was judged. threshold_applied exists so a false accept
        # can be attributed afterwards, and a button press must never read as
        # the detector having cleared its bar.
        msg.threshold_applied = 0.0
        msg.person_present = False
        self._wake_pub.publish(msg)
        return Result(ok=True, message="listening")

    async def robot_ask(self, text: str) -> Result:
        """Put a typed question into the robot's conversation; it answers aloud."""
        from neo_msgs.msg import Transcript

        text = (text or "").strip()
        if not text:
            return Result(ok=False, message="text must not be empty")
        if len(text) > MAX_ROBOT_TEXT:
            return Result(ok=False, message=f"text must be under {MAX_ROBOT_TEXT} characters")
        if self._state.dialog_state in ("THINKING", "SPEAKING"):
            # The dialog node drops a mid-turn transcript silently (one turn at a
            # time, no queue); saying so here beats a question that vanishes.
            return Result(ok=False, message="Neo is already answering; ask again when it has finished")
        msg = Transcript()
        msg.header.stamp = self._stamp()
        msg.text = text
        msg.is_final = True
        # Typed, not heard. The contract's only on-robot engine is Vosk; a 1.0
        # confidence, which no recogniser reports, is what marks it as typed.
        msg.confidence = 1.0
        msg.engine = Transcript.ENGINE_VOSK
        msg.grammar_constrained = False
        self._transcript_pub.publish(msg)
        return Result(ok=True, message="asked")

    async def robot_say(self, text: str) -> Result:
        """Speak `text` through whatever the robot's speaker currently is."""
        from neo_msgs.msg import Reply

        text = (text or "").strip()
        if not text:
            return Result(ok=False, message="text must not be empty")
        if len(text) > MAX_ROBOT_TEXT:
            return Result(ok=False, message=f"text must be under {MAX_ROBOT_TEXT} characters")
        msg = Reply()
        msg.header.stamp = self._stamp()
        msg.text = text
        # Operator-typed text is local and involves no model; KB is the source
        # whose meaning -- "not the language model" -- is true of it.
        msg.source = Reply.SOURCE_KB
        msg.latency_ms = 0.0
        self._reply_pub.publish(msg)
        return Result(ok=True, message="speaking")

    async def robot_cancel(self) -> Result:
        from std_msgs.msg import Empty

        self._cancel_pub.publish(Empty())
        return Result(ok=True, message="stopped")

    async def trigger_gesture(self, kind: str) -> Result:
        from std_msgs.msg import String

        kind = (kind or "").strip().lower()
        if kind not in GESTURES:
            return Result(ok=False, message=f"unknown gesture {kind!r}; one of {', '.join(GESTURES)}")
        if self._state.head.estop:
            return Result(ok=False, message="estop engaged")
        self._gesture_pub.publish(String(data=kind))
        return Result(ok=True, message=kind)

    # -- media in (panel -> robot) ------------------------------------------

    async def publish_joy(self, axes: list[float], buttons: list[int]) -> None:
        from sensor_msgs.msg import Joy

        msg = Joy()
        msg.header.stamp = self._stamp()
        msg.axes = [float(a) for a in axes]
        msg.buttons = [int(b) for b in buttons]
        self._joy_pub.publish(msg)

    async def publish_camera_frame(self, jpeg: bytes) -> None:
        from sensor_msgs.msg import Image

        # Pre-encoded JPEG in a generic Image, not decoded to bgr8: the browser
        # already sends JPEG, and decoding it here only for the perception node
        # to receive it again would cost a frame's worth of CPU twice over.
        # perception_node decodes "jpeg" itself.
        msg = Image()
        msg.header.stamp = self._stamp()
        msg.encoding = "jpeg"
        msg.data = list(jpeg)
        self._camera_pub.publish(msg)

    async def publish_mic_chunk(self, pcm_s16le: bytes) -> None:
        from neo_msgs.msg import AudioChunk

        msg = AudioChunk()
        msg.header.stamp = self._stamp()
        msg.seq = self._mic_seq
        self._mic_seq = (self._mic_seq + 1) & 0xFFFFFFFF
        msg.sample_rate = 16000
        msg.channels = 1
        msg.encoding = AudioChunk.ENCODING_PCM_S16LE
        msg.data = list(pcm_s16le)
        self._mic_pub.publish(msg)
