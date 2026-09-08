"""ROS 2 node wrapping chat/asr/tts -- same thin-wrapper discipline as
model_conn/nodes/link_node.py and neo_perception/nodes/perception_node.py.

Activates once Phase 1 lands `neo_msgs`. Until then, `neo --prompt` exercises
chat.ask() directly, and status_store.py's local JSON file is what
neo_webapp reads (see neo_webapp/dialog_status.py).
"""

from __future__ import annotations

import logging

from ..chat import ask
from ..config import Config

log = logging.getLogger("intelligence.nodes.dialog_node")


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


class DialogNode:
    """Wiring plan (contracts in docs/IMPLEMENTATION_PLAN.md section 1.2 / 5).

    subscribe  /dialog/transcript   neo_msgs/Transcript   (from asr_router)
    publish    /dialog/state        neo_msgs/DialogState  (IDLE/THINKING/SPEAKING/...)
               /audio/out           neo_msgs/AudioChunk   (from tts.synthesize)

    Each transcript calls chat.ask() and publishes the reply's audio via
    tts.synthesize(), same core functions `neo --prompt` and a future ASR
    pipeline both call -- this class only adds the ROS plumbing.

    TODO(phase-1): bind the actual rclpy pub/sub once neo_msgs/DialogState and
    neo_msgs/Transcript exist; this class is not instantiated by any
    console_script yet.
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg

    def on_transcript(self, text: str, mc_cfg) -> str:
        result = ask(text, cfg=self._cfg, mc_cfg=mc_cfg)
        return result.reply
