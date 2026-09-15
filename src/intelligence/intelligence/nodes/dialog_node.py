"""The conversation state machine: wake -> listen -> answer -> speak.

Owns /dialog/state, which is the one place anything else asks what Neo is
doing. The wake word mutes itself on it, the emotion layer reads it, and the
admin panel shows it.

One turn:

    /wake/event        -> LISTENING   (asr_router opens its window)
    /dialog/transcript -> THINKING    (final, non-empty)
    /dialog/reply      -> SPEAKING    (tts renders it; /dialog/speaking ends it)

Four deliberate shapes, each of which was tempting to do the simpler way:

* **Answering runs on a worker thread.** `chat.ask()` can take a minute -- the
  model host is a laptop that may be cold or absent, and degraded is the normal
  operating state, not an error path (CLAUDE.md). Answering inside the
  subscription callback stalls this node's executor, and what it stalls is
  /dialog/state -- so everything downstream would still believe Neo was
  THINKING long after it had given up.
* **Speech is another node's job.** This publishes `/dialog/reply`; `neo_audio`'s
  tts node synthesises it. Piper blocks for seconds on a paragraph, and keeping
  that in a different process is what keeps this state machine answering.
* **One turn at a time, no queue** (CLAUDE.md, plan 5.6). A transcript that
  arrives mid-turn is dropped rather than stacked up and answered once the
  moment it belonged to has passed.
* **Every state has an exit that does not depend on another node.** The wake
  word is gated off whenever this state is not IDLE, so a turn stuck in
  LISTENING, THINKING or SPEAKING -- because asr_router, tts or the speaker died
  mid-turn -- would leave the robot deaf until someone restarted it. A watchdog
  returns to IDLE after a bound on each (plan 8, step 2), and the state is
  republished twice a second, so a node that restarts or misses a transition
  re-learns it rather than waiting for the next one.
"""

from __future__ import annotations

import logging
import threading
import time

from model_conn.config import Config as ModelConnConfig

from ..chat import ask
from ..config import Config
from ..intents import describe_object, no_object_seen, wants_object_identification

log = logging.getLogger("intelligence.nodes.dialog_node")

try:
    from rclpy.node import Node
except ImportError:  # pragma: no cover - exercised only where ROS is installed
    Node = object

# neo_msgs/DialogState constants, mirrored as plain ints so this module parses
# without neo_msgs installed (see probe()).
_IDLE, _LISTENING, _THINKING, _SPEAKING = 0, 1, 2, 3
_NAMES = {_IDLE: "IDLE", _LISTENING: "LISTENING", _THINKING: "THINKING", _SPEAKING: "SPEAKING"}

# neo_msgs/Reply source constants, same reasoning.
_SOURCE_KB, _SOURCE_LLM, _SOURCE_KB_POLISHED, _SOURCE_FALLBACK = 0, 1, 2, 3

_REPLY_SOURCE = {
    # chat.ChatResult.source is "ollama" | "kb" | "degraded".
    "ollama": _SOURCE_LLM,
    "kb": _SOURCE_KB,
    "degraded": _SOURCE_FALLBACK,
}

IDENTIFY_WAIT_S = 6.0
"""Ceiling on the /perception/identify round trip.

Deliberately longer than perception's own timeout, so its considered answer
wins the race; this only catches a perception node that is not running at all.
"""

LISTENING_TIMEOUT_S = 25.0
"""asr_router closes its own window -- within 15 s of speech, or by its own
watchdog when audio stops -- so this only catches an asr_router that is not
running at all."""

REPLY_START_TIMEOUT_S = 10.0
"""From publishing a reply to tts reporting that speech has started. The first
sentence takes well under a second once Piper is loaded; ten covers a cold load."""

SPEAKING_TIMEOUT_S = 120.0
"""The longest a reply may be heard for. A 600-character answer is under a minute."""

HEARTBEAT_S = 0.5


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


class DialogNode(Node):
    """Wiring plan (contracts in docs/IMPLEMENTATION_PLAN.md sections 1.2, 5, 8).

    subscribe  /wake/event         neo_msgs/WakeEvent
               /dialog/transcript  neo_msgs/Transcript
               /dialog/speaking    std_msgs/Bool         (from tts)
               /link/health        neo_msgs/LinkHealth   -> DialogState.degraded
    publish    /dialog/state       neo_msgs/DialogState  (on change, and every 0.5 s)
               /dialog/reply       neo_msgs/Reply
    client     /perception/identify  std_srvs/Trigger
    """

    def __init__(self, cfg: Config, mc_cfg: ModelConnConfig) -> None:
        ok, why = probe()
        if not ok:
            raise RuntimeError(f"ROS dialog node unavailable: {why}")

        from neo_msgs.msg import DialogState, LinkHealth, Reply, Transcript, WakeEvent
        from rclpy.callback_groups import ReentrantCallbackGroup
        from std_msgs.msg import Bool
        from std_srvs.srv import Trigger

        super().__init__("dialog")

        self._cfg = cfg
        self._mc_cfg = mc_cfg
        self._busy = False
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._degraded = False
        self._state = _IDLE
        self._state_since = time.monotonic()
        self._reply_published_at: float | None = None

        group = ReentrantCallbackGroup()
        self.create_subscription(WakeEvent, "/wake/event", self._on_wake, 10)
        self.create_subscription(
            Transcript, "/dialog/transcript", self._on_transcript, 10, callback_group=group
        )
        self.create_subscription(Bool, "/dialog/speaking", self._on_speaking, 10)
        self.create_subscription(LinkHealth, "/link/health", self._on_link, 10)

        self._state_pub = self.create_publisher(DialogState, "/dialog/state", 10)
        self._reply_pub = self.create_publisher(Reply, "/dialog/reply", 10)
        self._identify = self.create_client(
            Trigger, "/perception/identify", callback_group=group
        )
        self.create_timer(HEARTBEAT_S, self._on_watchdog)

        self._publish_state(_IDLE)
        self.get_logger().info("dialog: idle, waiting on /wake/event")

    # -- core, the same call `neo --prompt` makes ---------------------------

    def answer(self, text: str) -> tuple[str, int, float]:
        """A question in, an answer plus its provenance and latency out.

        The identification branch is checked first and never reaches the model
        host: "what is this?" is a question about the room, and the laptop
        cannot see the room.
        """
        started = time.monotonic()
        if wants_object_identification(text):
            label, ok = self._ask_perception()
            reply = describe_object(label) if ok else no_object_seen()
            return reply, _SOURCE_KB, (time.monotonic() - started) * 1000.0

        result = ask(text, cfg=self._cfg, mc_cfg=self._mc_cfg)
        source = _REPLY_SOURCE.get(result.source, _SOURCE_FALLBACK)
        return result.reply, source, result.latency_ms or (time.monotonic() - started) * 1000.0

    def _ask_perception(self) -> tuple[str, bool]:
        from std_srvs.srv import Trigger

        if not self._identify.wait_for_service(timeout_sec=1.0):
            self.get_logger().warning("identify: the perception node is not running")
            return "", False
        future = self._identify.call_async(Trigger.Request())
        # Blocking here is safe: this runs on the answering worker thread, and
        # the client is in a reentrant group, so the executor keeps turning and
        # can deliver the response that ends this wait.
        deadline = time.monotonic() + IDENTIFY_WAIT_S
        while not future.done() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not future.done() or future.result() is None:
            self.get_logger().warning("identify: no answer from perception")
            return "", False
        response = future.result()
        return (response.message or ""), bool(response.success)

    # -- ROS callbacks -------------------------------------------------------

    def _on_wake(self, msg) -> None:
        with self._lock:
            if self._busy:
                return
        self._publish_state(_LISTENING)
        self.get_logger().info(
            f"wake: {msg.score:.3f} >= {msg.threshold_applied:.2f}"
            f"{' (person in frame)' if msg.person_present else ''}"
        )

    def _on_link(self, msg) -> None:
        # No model host is the *normal* state on this robot, not a fault
        # (CLAUDE.md); it changes what the panel shows, nothing else.
        self._degraded = not bool(getattr(msg, "up", False))

    def _on_speaking(self, msg) -> None:
        if msg.data:
            self._reply_published_at = None
            self._publish_state(_SPEAKING)
        elif self._state == _SPEAKING:
            self._publish_state(_IDLE)

    def _on_transcript(self, msg) -> None:
        if not msg.is_final:
            return
        text = (msg.text or "").strip()
        if not text:
            # The window closed having heard nothing: a false wake. Return to
            # idle rather than leaving LISTENING latched, which would keep the
            # wake word gated off.
            if self._state == _LISTENING:
                self._publish_state(_IDLE)
            return

        with self._lock:
            if self._busy:
                self.get_logger().warning("dropping transcript: a reply is already in flight")
                return
            self._busy = True

        threading.Thread(
            target=self._answer_turn, args=(text,), name="dialog-turn", daemon=True
        ).start()

    def _answer_turn(self, text: str) -> None:
        try:
            self._publish_state(_THINKING)
            reply, source, latency_ms = self.answer(text)
            if reply:
                self._publish_reply(reply, source, latency_ms)
            else:
                self._publish_state(_IDLE)
        except Exception:  # noqa: BLE001 - one bad turn must not end the conversation
            self.get_logger().error("answering failed", exc_info=True)
            self._publish_state(_IDLE)
        finally:
            with self._lock:
                self._busy = False

    def _on_watchdog(self) -> None:
        """Bound every state, then republish the current one."""
        now = time.monotonic()
        state, since, reply_at = self._state, self._state_since, self._reply_published_at
        reason = None
        if state == _LISTENING and now - since > LISTENING_TIMEOUT_S:
            reason = "no transcript arrived -- is asr_router running?"
        elif state == _THINKING and reply_at is not None and now - reply_at > REPLY_START_TIMEOUT_S:
            reason = "the reply was never spoken -- is tts running?"
        elif state == _SPEAKING and now - since > SPEAKING_TIMEOUT_S:
            reason = "speech never reported finishing"
        # THINKING with no reply yet is the model host taking its time, which
        # chat.timeout_s already bounds; cutting it short here would throw away
        # an answer that is still coming.

        if reason is not None:
            self._reply_published_at = None
            self.get_logger().warning(f"dialog: back to IDLE from {_NAMES.get(state, state)}: {reason}")
            self._publish_state(_IDLE)
            return
        self._republish()

    # -- publishing ----------------------------------------------------------

    def _publish_reply(self, text: str, source: int, latency_ms: float) -> None:
        from neo_msgs.msg import Reply

        msg = Reply()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.text = text
        msg.source = source
        msg.latency_ms = float(latency_ms)
        self._reply_published_at = time.monotonic()
        self._reply_pub.publish(msg)
        self.get_logger().info(f"reply ({latency_ms:.0f} ms): {text[:80]}")
        # SPEAKING is *not* published here. It is published when tts says it
        # has started: the gap between publishing a reply and audio leaving the
        # speaker is exactly where a half-duplex gate built on this message
        # would let the microphone hear Neo's first sentence.

    def _publish_state(self, state: int) -> None:
        # Under one lock with the publish itself, so a transition from the
        # answering thread and a heartbeat from the executor can never go out
        # in the wrong order and leave the last word with the stale state.
        with self._state_lock:
            if state != self._state:
                self._state_since = time.monotonic()
            self._state = state
            self._emit(state)

    def _republish(self) -> None:
        with self._state_lock:
            self._emit(self._state)

    def _emit(self, state: int) -> None:
        from neo_msgs.msg import DialogState

        msg = DialogState()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.state = state
        msg.degraded = self._degraded
        msg.estop = False
        msg.follow_up_open = False
        self._state_pub.publish(msg)


def main(argv: list[str] | None = None) -> int:
    ok, why = probe()
    if not ok:
        print(f"cannot start dialog node: {why}")
        print("Use 'neo --prompt \"...\"' instead -- it drives chat.ask() directly.")
        return 1

    import rclpy
    from rclpy.executors import MultiThreadedExecutor

    rclpy.init(args=argv)
    node = DialogNode(Config.load(), ModelConnConfig.load())
    # Multi-threaded because a turn waits on /perception/identify while this
    # same executor has to keep servicing the response that ends the wait.
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    import signal

    from rclpy.executors import ExternalShutdownException

    try:
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        # A second SIGINT (the process group's, then launch's) must not cut the
        # shutdown short; see neo_motion/nodes/head_behavior.py.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0
