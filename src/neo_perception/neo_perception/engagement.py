"""Who the head is locked onto, and when it lets go.

    SCANNING  --gesture-->  ENGAGING  --held-->  ENGAGED
       ^                       |                  |
       |                       | gesture dropped  | target not visible
       |                       v                  v
       +---------------------------------------- SUSPENDED
                    grace expired / track gone

Two grace periods, not one
--------------------------
"The target vanished" has two very different causes, and treating them alike makes
the robot feel broken either way:

* **Walked out of shot.** Last box was against a frame edge. They have left; hold
  briefly, then release, so the next person can get the robot's attention. A long
  grace here means the head stares at a doorway.
* **Detector blinked, or someone walked in front of them.** Last box was in open
  frame. They are almost certainly still there; a short grace here means the lock
  drops every time the detector misses a frame, which at 3-5 fps is often.

Re-acquisition is by track id, so the grace can never usefully outlive the
tracker's own memory -- see `validate_against_tracker`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .tracker import TrackerConfig
from .types import (
    EngagementDecision,
    EngagementState,
    GestureKind,
    Track,
)


@dataclass
class EngagementConfig:
    engage_gesture: GestureKind = GestureKind.RAISED_HAND
    """RAISED_HAND by default: it survives the Pi's frame rate, WAVE does not."""

    confirm_s: float = 0.6
    """How long the gesture must be held before locking.

    Guards against a lock triggered by someone scratching their head or reaching
    for a shelf. Too long and the interaction feels unresponsive; 0.6 s is roughly
    two or three frames at Pi rates.
    """

    exit_grace_s: float = 0.6
    """Release delay when the target was last seen at a frame edge."""

    occlusion_grace_s: float = 2.0
    """Release delay when the target vanished from open frame."""

    border_margin_px: float = 8.0

    max_engage_s: float = 0.0
    """Hard cap on one engagement, 0 to disable.

    A receptionist that has been locked onto one person for ten minutes has
    almost certainly latched onto a coat stand.
    """

    scan_gaze: bool = True
    """While unengaged, still report the most salient person at low confidence, so
    the emotion layer can decide whether to glance. Engagement is what makes the
    head *follow*; presence only makes it *notice*."""


class EngagementController:
    def __init__(self, config: EngagementConfig | None = None) -> None:
        self.cfg = config or EngagementConfig()
        self.state = EngagementState.SCANNING
        self.target_id: int | None = None
        self._candidate_id: int | None = None
        self._candidate_since: float = 0.0
        self._engaged_since: float = 0.0
        self._suspended_since: float = 0.0
        self._grace_s: float = 0.0
        self._last_target_bbox = None
        self.last_release_reason: str = ""

    # -- configuration sanity ---------------------------------------------

    def validate_against_tracker(self, tracker_cfg: TrackerConfig) -> list[str]:
        """Grace periods longer than the tracker's memory are silently useless.

        Re-acquisition matches on track id; once the tracker has dropped the track
        the id is gone, so the remaining grace can never succeed.
        """
        problems = []
        longest = max(self.cfg.exit_grace_s, self.cfg.occlusion_grace_s)
        if longest > tracker_cfg.max_age_s:
            problems.append(
                f"grace {longest:.1f}s exceeds tracker max_age_s "
                f"{tracker_cfg.max_age_s:.1f}s; the track is deleted first, so the "
                f"extra time can never re-acquire"
            )
        return problems

    # -- main update -------------------------------------------------------

    def update(
        self,
        tracks: list[Track],
        gestures: dict[int, GestureKind],
        stamp: float,
        frame_width: int,
        frame_height: int,
    ) -> EngagementDecision:
        visible = {t.track_id: t for t in tracks if t.confirmed}
        person_present = bool(visible)

        if self.state in (EngagementState.SCANNING, EngagementState.ENGAGING):
            self._step_unengaged(visible, gestures, stamp)
        elif self.state is EngagementState.ENGAGED:
            self._step_engaged(visible, stamp, frame_width, frame_height)
        elif self.state is EngagementState.SUSPENDED:
            self._step_suspended(visible, stamp)

        return self._decide(visible, person_present)

    def _step_unengaged(
        self,
        visible: dict[int, Track],
        gestures: dict[int, GestureKind],
        stamp: float,
    ) -> None:
        # An existing candidate must still be visible and still gesturing.
        if self._candidate_id is not None:
            still_gesturing = (
                self._candidate_id in visible
                and gestures.get(self._candidate_id) is self.cfg.engage_gesture
            )
            if not still_gesturing:
                self._candidate_id = None
                self.state = EngagementState.SCANNING
            elif (stamp - self._candidate_since) >= self.cfg.confirm_s:
                self._engage(self._candidate_id, visible, stamp)
                return

        if self._candidate_id is None:
            candidate = self._pick_gesturing(visible, gestures)
            if candidate is not None:
                self._candidate_id = candidate
                self._candidate_since = stamp
                self.state = EngagementState.ENGAGING
                # A zero-length confirmation should lock on this same frame.
                if self.cfg.confirm_s <= 0.0:
                    self._engage(candidate, visible, stamp)

    def _pick_gesturing(
        self, visible: dict[int, Track], gestures: dict[int, GestureKind]
    ) -> int | None:
        """Nearest gesturing person wins, by box area."""
        gesturing = [
            tid
            for tid, kind in gestures.items()
            if kind is self.cfg.engage_gesture and tid in visible
        ]
        if not gesturing:
            return None
        return max(gesturing, key=lambda tid: visible[tid].bbox.area)

    def _engage(self, track_id: int, visible: dict[int, Track], stamp: float) -> None:
        self.state = EngagementState.ENGAGED
        self.target_id = track_id
        self._engaged_since = stamp
        self._candidate_id = None
        track = visible.get(track_id)
        if track is not None:
            self._last_target_bbox = track.bbox

    def _step_engaged(
        self,
        visible: dict[int, Track],
        stamp: float,
        frame_width: int,
        frame_height: int,
    ) -> None:
        if self.cfg.max_engage_s > 0 and (stamp - self._engaged_since) >= self.cfg.max_engage_s:
            self._release("max engagement time reached")
            return

        track = visible.get(self.target_id)
        if track is not None:
            self._last_target_bbox = track.bbox
            return

        # Lost this frame: pick the grace period from *where* it was last seen.
        left_frame = self._last_target_bbox is not None and self._last_target_bbox.touches_border(
            frame_width, frame_height, self.cfg.border_margin_px
        )
        self._grace_s = self.cfg.exit_grace_s if left_frame else self.cfg.occlusion_grace_s
        self._suspended_since = stamp
        self.state = EngagementState.SUSPENDED

    def _step_suspended(self, visible: dict[int, Track], stamp: float) -> None:
        if self.target_id in visible:
            self.state = EngagementState.ENGAGED
            self._last_target_bbox = visible[self.target_id].bbox
            return
        if (stamp - self._suspended_since) >= self._grace_s:
            self._release(
                "left the frame"
                if self._grace_s == self.cfg.exit_grace_s
                else "lost from view"
            )

    # -- external control --------------------------------------------------

    def release(self, reason: str = "released") -> None:
        """Drop the lock -- panel override, estop, or end of conversation."""
        self._release(reason)

    def _release(self, reason: str) -> None:
        self.state = EngagementState.SCANNING
        self.target_id = None
        self._candidate_id = None
        self._last_target_bbox = None
        self.last_release_reason = reason

    def reset(self) -> None:
        self._release("reset")

    # -- output ------------------------------------------------------------

    def _decide(
        self, visible: dict[int, Track], person_present: bool
    ) -> EngagementDecision:
        engaged = self.state in (EngagementState.ENGAGED, EngagementState.SUSPENDED)

        track: Track | None = None
        confidence = 0.0
        if engaged:
            track = visible.get(self.target_id)
            # While suspended there is nothing to aim at; the head holds its last
            # command rather than snapping to somebody else.
            confidence = 1.0 if track is not None else 0.0
        elif self.cfg.scan_gaze and visible:
            track = max(visible.values(), key=lambda t: t.bbox.area)
            confidence = 0.35

        return EngagementDecision(
            state=self.state,
            target=track,
            track_id=self.target_id if engaged else (track.track_id if track else None),
            confidence=confidence,
            engaged=engaged,
            person_present=person_present,
        )
