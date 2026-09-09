"""Who the head is locked onto, and when it lets go.

    SCANNING  --gesture-->  ENGAGING  --held-->  ENGAGED
       ^                       |                  |
       |                       | gesture dropped  | target not visible
       |                       v                  v
       +---------------------------------------- SUSPENDED
                    grace expired / track gone

Three ways to lose someone, three different timings
---------------------------------------------------
"The target is gone" has causes that look identical to the code and demand
opposite handling. Collapsing them into one timeout makes the robot feel broken
whichever value you pick:

* **Walked out of shot** (last box against a frame edge, 0.6 s). They have left;
  release soon so the next person can get the robot's attention. A long grace
  here means the head stares at a doorway.
* **Detector blinked, or someone crossed in front** (last box in open frame,
  2.0 s). They are almost certainly still there. A short grace here drops the
  lock every time the detector misses a frame, which at 3-5 fps is often.
* **Turned their back** (still tracked, still in frame, 1.5 s). Nothing above
  catches this: the person is perfectly visible and the lock would hold until
  they happened to walk out of shot. Facing comes from keypoints -- see
  `Keypoints.facing`.

Re-acquisition is by track id, so a grace can never usefully outlive the
tracker's own memory -- see `validate_against_tracker`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .tracker import TrackerConfig
from .types import (
    EngagementDecision,
    EngagementState,
    FacingState,
    GestureKind,
    Track,
)


@dataclass
class EngagementConfig:
    engage_gesture: GestureKind = GestureKind.OPEN_PALM
    """A presented palm: hand up, forearm vertical.

    OPEN_PALM rather than RAISED_HAND because the verticality requirement is
    what rejects a stretch or a reach for a shelf. Both survive the Pi's frame
    rate, being static poses; WAVE does not.
    """

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

    turned_away_grace_s: float = 1.5
    """How long the target may face away before the lock releases.

    A third way to lose someone, distinct from the two above: they are still in
    frame and still tracked, they have simply turned their back and walked off
    to their lecture. Without this the robot keeps solemnly tracking the back of
    a head until they happen to exit the frame.

    The delay matters as much as the rule. People turn away constantly
    mid-conversation -- to point at a corridor, to talk to a friend, to look at
    what the robot is pointing at. Releasing on the first away-facing frame
    would make the lock feel like it keeps dropping for no reason.
    """

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
        self._away_since: float | None = None
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
            self._update_facing(track, stamp)
            return

        # Lost this frame: pick the grace period from *where* it was last seen.
        left_frame = self._last_target_bbox is not None and self._last_target_bbox.touches_border(
            frame_width, frame_height, self.cfg.border_margin_px
        )
        self._grace_s = self.cfg.exit_grace_s if left_frame else self.cfg.occlusion_grace_s
        self._suspended_since = stamp
        self.state = EngagementState.SUSPENDED

    def _update_facing(self, track: Track, stamp: float) -> None:
        """Release once the target has faced away long enough to mean it.

        PROFILE deliberately does not count: someone glancing down a corridor,
        or turning to look at what the robot is looking at, is still in the
        conversation. Only a genuine AWAY starts the clock.
        """
        if track.facing is not FacingState.AWAY:
            self._away_since = None
            return
        if self._away_since is None:
            self._away_since = stamp
        elif (stamp - self._away_since) >= self.cfg.turned_away_grace_s:
            self._release("turned away")

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
        self._away_since = None
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
