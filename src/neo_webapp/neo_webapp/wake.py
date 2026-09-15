"""Runs the wake word "NEO" for the admin panel.

The relationship to `neo_audio.wakeword` is the same one `speech.py` has to
`intelligence`: the panel drives the real detector -- the numpy network that
runs on the robot -- against a browser mic, so the wake word can be exercised
end to end with no Pi, no ROS and no hardware at all. The detector's docstring
says it outright: it is deliberately not a node, so the panel can drive this
same class.

Optional by construction, like SpeechLink. If `neo_audio` is missing or no
model path is configured, the panel keeps working and reports *why* the wake
word is off rather than failing a request with a traceback.

The half-duplex rule is enforced at the feed site, not here: while Neo's own
voice is playing, the detectors are not fed (channels.py), so it can never
wake itself. It shares the browser mic's 16 kHz, exactly as on the robot.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from .bridge.types import WakeView

log = logging.getLogger(__name__)

try:
    from neo_audio.config import Config as AudioConfig
    from neo_audio.wakeword import WakeWordDetector, availability

    WAKE_AVAILABLE = True
except ImportError as exc:  # pragma: no cover - depends on environment
    log.info("neo_audio unavailable (%s); the wake word will be inert", exc)
    WAKE_AVAILABLE = False

# How far a detector may fall silent before the panel reads it as "not
# listening" rather than as silence -- mirrors the ROS bridge's freshness check.
SCORE_FRESH_S = 2.0


class WakeLink:
    """Adapts the panel's mic stream to the real wake word detector.

    One instance per panel, held on `app.state.wake` just like `app.state.speech`.
    It owns the model and the detector, accumulates fires over the session, and
    renders the running view the Audio tab reads at the state broadcast rate.
    """

    def __init__(
        self, *, mic_rate: int = 16000, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.available = WAKE_AVAILABLE
        self.mic_rate = mic_rate
        self._clock = clock

        self._cfg = None
        self._model = None
        self._detector: WakeWordDetector | None = None
        self._avail = None  # (ok, reason) from the last availability check
        self._last_error = ""

        self._fired = 0
        self._last_fired_at: float | None = None
        self._last_threshold = 0.0
        self._last_person_present = False
        self._last_feed_at = 0.0

    # -- config ------------------------------------------------------------

    def reload_config(self) -> None:
        """Pick up an edit to audio.local.yaml without a restart.

        Drops the loaded detector too: the point of reloading is usually that
        the model path or threshold changed, and keeping the old one would be
        the opposite of what was asked.
        """
        self._cfg = None
        self._model = None
        self._detector = None
        self._avail = None
        self._fired = 0
        self._last_fired_at = None
        self._last_threshold = 0.0

    # -- availability ------------------------------------------------------

    def refresh_availability(self) -> None:
        """Re-stat the model path. Called from the slow status task.

        Reads the file system, so it never runs in `view()`.
        """
        if not WAKE_AVAILABLE:
            return
        try:
            cfg = self._config()
            ok, reason = availability(cfg.wakeword)
            self._avail = (ok, reason)
            if ok and self._model is None:
                self._ensure_model()
        except Exception as exc:  # noqa: BLE001 - a bad config must not kill the task
            log.warning("wake word availability check failed: %s", exc)
            self._avail = None
            self._last_error = str(exc)

    def _ensure_model(self) -> None:
        cfg = self._config()
        self._model = _load_model(cfg)
        self._detector = WakeWordDetector(self._model, cfg.wakeword, sample_rate=self.mic_rate)

    def _config(self):
        if self._cfg is None:
            self._cfg = AudioConfig.load()
        return self._cfg

    # -- view ---------------------------------------------------------------

    def view(self) -> WakeView:
        """The Audio tab's wake word state. Cheap: no file I/O.

        `available` reads as the detector hearing audio *now*, not merely being
        configured: a model on disk that nobody has fed for a while is
        indistinguishable from silence unless this says so. The same freshness
        rule the ROS bridge applies to `/wake/score` messages.
        """
        now = self._clock()
        hearing = (
            self._avail is not None
            and self._avail[0]
            and self._detector is not None
            and (now - self._last_feed_at) < SCORE_FRESH_S
        )
        return WakeView(
            available=hearing,
            reason="" if hearing else self.reason(),
            score=float(self._detector.last_score) if self._detector is not None else 0.0,
            fired=self._fired,
            last_score=float(self._detector.last_score) if self._detector is not None else 0.0,
            last_threshold=self._last_threshold,
            last_person_present=self._last_person_present,
            last_fired_age_s=None if self._last_fired_at is None else now - self._last_fired_at,
        )

    def reason(self) -> str:
        """Why the wake word is off, if it is. "" when it should run."""
        if not WAKE_AVAILABLE:
            return "neo_audio is not installed -- pip install -e '.[detector,voice]' from the repo root"
        if self._avail is None:
            return self._last_error or "checking..."
        ok, reason = self._avail
        if ok:
            return ""
        return reason

    # -- the detector -------------------------------------------------------

    def feed(self, pcm: bytes, *, person_present: bool = False) -> None:
        """Feed one mic chunk to the real detector; record anything it fired.

        Synchronous on purpose: the forward pass is ~11 MFLOPs at ~7 Hz, far
        under a frame budget, and the detector is not safe to enter twice at
        once -- so there is nothing a worker thread would buy except a lock.
        """
        if self._detector is None:
            return
        self._last_feed_at = self._clock()
        try:
            detection = self._detector.accept(
                pcm, self._clock(), person_present=person_present
            )
        except Exception as exc:  # noqa: BLE001 - a bad chunk must not kill the mic
            self._last_error = f"wake word: {exc}"
            log.warning("wake word rejected a chunk: %s", exc)
            return
        if detection.fired:
            self._record_fire(detection.threshold_applied, detection.person_present)

    def manual_wake(self) -> None:
        """Open the listening window as if the word had been heard.

        The operator's push-to-talk, the same idea as `/api/robot/wake` on the
        ROS bridge: the fire carries a threshold of 0, so it can never be
        mistaken for the detector having accepted something.
        """
        self._record_fire(0.0, False)

    def _record_fire(self, threshold: float, person_present: bool) -> None:
        self._fired += 1
        self._last_fired_at = self._clock()
        self._last_threshold = threshold
        self._last_person_present = person_present

    def reset(self) -> None:
        """Clear fires and the detector's buffer; keep the loaded model."""
        if self._detector is not None:
            self._detector.reset()
        self._fired = 0
        self._last_fired_at = None
        self._last_threshold = 0.0
        self._last_person_present = False


def _load_model(cfg):
    from neo_audio.wakeword import load_model

    return load_model(cfg.wakeword_model_path)