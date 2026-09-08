"""Gesture recognition from pose keypoints.

No second model and no second inference pass: everything here is derived from the
COCO-17 keypoints the pose detector already produced. On a Pi 4 that matters more
than accuracy -- a dedicated hand model (MediaPipe and friends) would roughly
double the per-frame cost of a pipeline that only has ~4 fps to give.

Frame-rate reality check
------------------------
A *static* pose held over several frames is reliable at 3-5 fps. A *dynamic*
gesture is not: a 2 Hz wave sampled at 4 fps aliases badly. WAVE is implemented
and works when the pipeline is fast enough, but RAISED_HAND is the default engage
gesture precisely because it survives the Pi's frame rate.
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import GestureEvent, GestureKind, Keypoints, Track


@dataclass
class GestureConfig:
    keypoint_min_score: float = 0.35
    """Below this a keypoint is treated as not visible at all."""

    raise_margin: float = 0.15
    """How far above the shoulder the wrist must be, in torso units.

    Scale-invariant by construction: the comparison is against the person's own
    shoulder, so it does not change with their distance from the camera.
    """

    wave_window_s: float = 2.0
    wave_min_direction_changes: int = 2
    wave_min_amplitude: float = 0.12
    """Horizontal travel per swing, in torso units."""

    history_limit: int = 24


class GestureRecognizer:
    """Stateless per frame except for the wrist history needed to see a wave."""

    def __init__(self, config: GestureConfig | None = None) -> None:
        self.cfg = config or GestureConfig()
        self._active: dict[int, GestureKind] = {}

    def reset(self) -> None:
        self._active.clear()

    def update(
        self, tracks: list[Track], stamp: float
    ) -> tuple[dict[int, GestureKind], list[GestureEvent]]:
        """Return the current gesture per track, plus newly started gestures."""
        current: dict[int, GestureKind] = {}
        events: list[GestureEvent] = []

        live_ids = {t.track_id for t in tracks}
        for gone in set(self._active) - live_ids:
            del self._active[gone]

        for track in tracks:
            if not track.confirmed or track.keypoints is None:
                continue
            self._record_wrist(track, stamp)

            kind, confidence = self._classify(track, stamp)
            if kind is GestureKind.NONE:
                self._active.pop(track.track_id, None)
                continue

            current[track.track_id] = kind
            # Edge-triggered: one event when a gesture starts, not one per frame.
            if self._active.get(track.track_id) is not kind:
                events.append(
                    GestureEvent(
                        track_id=track.track_id,
                        kind=kind,
                        confidence=confidence,
                        stamp=stamp,
                    )
                )
            self._active[track.track_id] = kind

        return current, events

    # -- classification ----------------------------------------------------

    def _classify(self, track: Track, stamp: float) -> tuple[GestureKind, float]:
        raised, confidence = self._hand_raised(track.keypoints)
        if not raised:
            return GestureKind.NONE, 0.0
        if self._is_waving(track, stamp):
            return GestureKind.WAVE, confidence
        return GestureKind.RAISED_HAND, confidence

    def _hand_raised(self, kps: Keypoints | None) -> tuple[bool, float]:
        if kps is None:
            return False, 0.0
        scale = kps.torso_scale(self.cfg.keypoint_min_score)
        if scale is None:
            return False, 0.0

        best = 0.0
        for side in ("left", "right"):
            wrist = kps.get(f"{side}_wrist", self.cfg.keypoint_min_score)
            shoulder = kps.get(f"{side}_shoulder", self.cfg.keypoint_min_score)
            if wrist is None or shoulder is None:
                continue
            # Image y grows downward, so "above" is a smaller y.
            lift = (shoulder.y - wrist.y) / scale
            if lift >= self.cfg.raise_margin:
                # Confident when the arm is clearly up and both joints are solid.
                strength = min(1.0, lift / (self.cfg.raise_margin * 3.0))
                best = max(best, strength * min(wrist.score, shoulder.score))
        return best > 0.0, best

    def _record_wrist(self, track: Track, stamp: float) -> None:
        kps = track.keypoints
        if kps is None:
            return
        scale = kps.torso_scale(self.cfg.keypoint_min_score)
        if scale is None:
            return

        # Track whichever wrist is raised, measured relative to its own shoulder so
        # the signal does not move when the person walks across the frame.
        for side in ("left", "right"):
            wrist = kps.get(f"{side}_wrist", self.cfg.keypoint_min_score)
            shoulder = kps.get(f"{side}_shoulder", self.cfg.keypoint_min_score)
            if wrist is None or shoulder is None:
                continue
            if (shoulder.y - wrist.y) / scale < self.cfg.raise_margin:
                continue
            track.wrist_history.append((stamp, (wrist.x - shoulder.x) / scale))
            break

        cutoff = stamp - self.cfg.wave_window_s
        track.wrist_history = [
            (t, x) for (t, x) in track.wrist_history if t >= cutoff
        ][-self.cfg.history_limit :]

    def _is_waving(self, track: Track, stamp: float) -> bool:
        history = [(t, x) for (t, x) in track.wrist_history if t >= stamp - self.cfg.wave_window_s]
        if len(history) < 4:
            return False

        direction = 0
        changes = 0
        last_extreme = history[0][1]
        for _, x in history[1:]:
            delta = x - last_extreme
            if abs(delta) < self.cfg.wave_min_amplitude:
                continue
            new_direction = 1 if delta > 0 else -1
            if direction != 0 and new_direction != direction:
                changes += 1
            direction = new_direction
            last_extreme = x

        return changes >= self.cfg.wave_min_direction_changes
