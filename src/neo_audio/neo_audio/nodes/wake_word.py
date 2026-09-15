"""The wake word gate. Nothing downstream runs until this fires.

Neo is silent and non-responsive until "NEO" is heard (CLAUDE.md). This node is
*upstream* of ASR, not a filter applied to its output: while it is waiting, no
transcript exists, nothing is sent to the model host, and the room's
conversation is read one second at a time and thrown away.

Three things it must not do, each of which has bitten a wake word somewhere:

* **Fire on Neo's own voice.** /audio/playing mutes the detector while the
  speaker is live, plus a tail: the reply's last word is still in the rolling
  window for a second after the audio stops.
* **Fire repeatedly on one utterance.** The windows overlap by ~85%, so
  without the refractory period in `WakeWordDetector` a single "NEO" fires
  seven times.
* **Ignore what the camera knows.** The threshold relaxes while perception
  reports a person in frame (plan 5.2). `WakeEvent.threshold_applied` carries
  the value actually used, because a false accept that cannot be attributed to
  a threshold cannot be tuned out afterwards.
"""

from __future__ import annotations

import logging

from ..config import Config
from ..wakeword import WakeWordDetector, load_model

log = logging.getLogger(__name__)

try:
    from rclpy.node import Node
except ImportError:  # pragma: no cover - exercised only where ROS is installed
    Node = object

PLAYBACK_TAIL_S = 1.2
"""Extra mute after the speaker goes quiet.

The detector's window is a whole second long, so the moment playback stops the
buffer still holds Neo's last word. Unmuting immediately would classify it.
"""

ATTENTION_FRESH_S = 1.0
"""How long a perception result counts as "someone is there" -- the same
staleness rule the admin panel applies to gaze (CLAUDE.md)."""


def probe() -> tuple[bool, str]:
    try:
        import rclpy  # noqa: F401
    except ImportError:
        return False, "rclpy not installed"
    try:
        import neo_msgs.msg  # noqa: F401
    except ImportError:
        return False, "neo_msgs not built (Phase 1)"
    return True, "ok"


class WakeWordNode(Node):
    """Wiring plan (contracts in docs/IMPLEMENTATION_PLAN.md section 5.2).

    subscribe  /audio/in               neo_msgs/AudioChunk
               /perception/attention   neo_msgs/AttentionTarget  -- relaxes the threshold
               /audio/playing          std_msgs/Bool             -- mutes while speaking
               /dialog/state           neo_msgs/DialogState      -- mutes unless IDLE
    publish    /wake/event             neo_msgs/WakeEvent
               /wake/score             std_msgs/Float32          -- the panel's meter
    """

    def __init__(self, config: Config | None = None) -> None:
        ok, why = probe()
        if not ok:
            raise RuntimeError(f"ROS wake word unavailable: {why}")

        from neo_msgs.msg import AttentionTarget, AudioChunk, DialogState, WakeEvent
        from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
        from std_msgs.msg import Bool, Float32

        super().__init__("wake_word")
        self._config = config or Config.load()

        path = self._config.wakeword_model_path
        if path is None:
            raise RuntimeError(
                "no wake word model configured. Set audio.wakeword.model_path in "
                "config/audio.local.yaml to the Teachable Machine export (.zip)."
            )
        model = load_model(path)
        self._detector = WakeWordDetector(
            model, self._config.wakeword, sample_rate=self._config.mic.sample_rate
        )

        self._person_present = False
        self._person_stamp = 0.0
        self._gated = False

        qos = QoSProfile(
            depth=1, reliability=ReliabilityPolicy.BEST_EFFORT, history=HistoryPolicy.KEEP_LAST
        )
        # Audio gets a queue, not latest-wins: a chunk dropped while a window
        # is being scored is a gap inside the next spectrogram, and a gap can
        # split "NEO" in two. Scoring takes milliseconds; ten chunks is a second.
        audio_qos = QoSProfile(
            depth=10, reliability=ReliabilityPolicy.BEST_EFFORT, history=HistoryPolicy.KEEP_LAST
        )
        self.create_subscription(AudioChunk, "/audio/in", self._on_audio, audio_qos)
        self.create_subscription(AttentionTarget, "/perception/attention", self._on_attention, qos)
        self.create_subscription(Bool, "/audio/playing", self._on_playing, 10)
        self.create_subscription(DialogState, "/dialog/state", self._on_dialog, 10)
        self._pub = self.create_publisher(WakeEvent, "/wake/event", 10)
        self._score_pub = self.create_publisher(Float32, "/wake/score", qos)

        self.get_logger().info(
            f"wake_word: '{model.labels.names[model.labels.wake_index]}' from {path.name}, "
            f"threshold {self._config.wakeword.threshold} "
            f"({self._config.wakeword.threshold_with_person} with a person in frame)"
        )

    def _now(self) -> float:
        return self.get_clock().now().nanoseconds / 1e9

    def _on_attention(self, msg) -> None:
        self._person_present = msg.confidence > 0.0
        self._person_stamp = self._now()

    def _on_playing(self, msg) -> None:
        if msg.data:
            # Re-armed on every tick while playing, so the tail is measured from
            # the *last* chunk rather than the first.
            self._detector.mute_until(self._now() + PLAYBACK_TAIL_S)

    def _on_dialog(self, msg) -> None:
        # Anything but IDLE means a turn is already under way: the window is
        # open, a reply is being composed, or one is being spoken. Waking again
        # mid-turn would interrupt Neo with itself.
        self._gated = msg.state != 0

    def _on_audio(self, msg) -> None:
        if self._gated:
            self._detector.reset()
            return
        now = self._now()
        person = self._person_present and (now - self._person_stamp) < ATTENTION_FRESH_S
        result = self._detector.accept(bytes(msg.data), now, person_present=person)
        self._publish_score(self._detector.last_score)
        if result.fired:
            self._publish_wake(result, now)

    def _publish_score(self, score: float) -> None:
        from std_msgs.msg import Float32

        self._score_pub.publish(Float32(data=float(score)))

    def _publish_wake(self, result, now: float) -> None:
        from neo_msgs.msg import WakeEvent

        msg = WakeEvent()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.score = float(result.score)
        msg.threshold_applied = float(result.threshold_applied)
        msg.person_present = bool(result.person_present)
        self._pub.publish(msg)
        self.get_logger().info(
            f"wake: {result.score:.3f} >= {result.threshold_applied:.2f}"
            f"{' (person in frame)' if result.person_present else ''}"
        )


def main(argv: list[str] | None = None) -> int:
    ok, why = probe()
    if not ok:
        print(f"cannot start wake word: {why}")
        return 1

    import rclpy

    rclpy.init(args=argv)
    try:
        node = WakeWordNode()
    except (RuntimeError, FileNotFoundError, ValueError) as exc:
        # A missing or broken model is a configuration problem with a specific
        # fix, so say what it is rather than dumping a traceback from inside
        # a weights parser.
        print(f"cannot start wake word: {exc}")
        rclpy.shutdown()
        return 1
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0
