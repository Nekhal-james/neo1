"""Multi-person tracking.

Deliberately simple: greedy IoU matching over a constant-velocity prediction, with
hysteresis before a track is trusted. No Kalman filter, no appearance embedding --
at the 3-5 fps a Pi 4 gives us, a heavier tracker would cost frames it cannot
spare, and the receptionist case (a few people, a fixed camera, short ranges) does
not need one.

Ages are in **seconds, not frames**, because the pipeline's frame rate varies with
CPU load. A frame-count timeout would silently mean different things at 3 fps and
at 8 fps.
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import BBox, Detection, Track


@dataclass
class TrackerConfig:
    iou_gate: float = 0.25
    """Minimum IoU to accept a match against the predicted box."""

    center_gate_scale: float = 1.2
    """Fallback match: centre distance below this multiple of the box diagonal.

    Catches the fast-motion case where prediction and detection no longer overlap
    at all, which happens easily at 3 fps.
    """

    min_hits: int = 2
    """Detections before a track is confirmed, so a one-frame flicker never
    becomes a lock candidate."""

    max_age_s: float = 2.5
    """How long an unmatched track survives before deletion."""

    max_velocity_px_s: float = 2000.0
    """Clamp, so one bad frame cannot fling a prediction off-screen."""


class MultiTracker:
    def __init__(self, config: TrackerConfig | None = None) -> None:
        self.cfg = config or TrackerConfig()
        self.tracks: list[Track] = []
        self._next_id = 1

    def reset(self) -> None:
        self.tracks.clear()
        self._next_id = 1

    def update(self, detections: list[Detection], stamp: float) -> list[Track]:
        matches, unmatched_dets = self._associate(detections, stamp)

        for track, det in matches:
            self._apply(track, det, stamp)

        matched_tracks = {id(t) for t, _ in matches}
        for track in self.tracks:
            if id(track) not in matched_tracks:
                track.misses += 1

        for det in unmatched_dets:
            self._spawn(det, stamp)

        self.tracks = [
            t for t in self.tracks if (stamp - t.last_seen) <= self.cfg.max_age_s
        ]
        return self.tracks

    # -- association -------------------------------------------------------

    def _associate(
        self, detections: list[Detection], stamp: float
    ) -> tuple[list[tuple[Track, Detection]], list[Detection]]:
        candidates: list[tuple[float, Track, Detection]] = []

        for track in self.tracks:
            dt = max(0.0, stamp - track.last_seen)
            predicted = track.predict(dt)
            for det in detections:
                iou = predicted.iou(det.bbox)
                if iou >= self.cfg.iou_gate:
                    candidates.append((iou, track, det))
                    continue
                # Fallback for fast motion, where boxes no longer overlap.
                diag = (predicted.width**2 + predicted.height**2) ** 0.5
                if diag <= 0:
                    continue
                dist = (
                    (predicted.cx - det.bbox.cx) ** 2
                    + (predicted.cy - det.bbox.cy) ** 2
                ) ** 0.5
                if dist <= self.cfg.center_gate_scale * diag:
                    # Score below any real IoU match, so overlap always wins.
                    candidates.append((0.01 * (1.0 - dist / diag), track, det))

        candidates.sort(key=lambda c: c[0], reverse=True)

        used_tracks: set[int] = set()
        used_dets: set[int] = set()
        matches: list[tuple[Track, Detection]] = []
        for _, track, det in candidates:
            if id(track) in used_tracks or id(det) in used_dets:
                continue
            used_tracks.add(id(track))
            used_dets.add(id(det))
            matches.append((track, det))

        unmatched = [d for d in detections if id(d) not in used_dets]
        return matches, unmatched

    # -- track maintenance -------------------------------------------------

    def _apply(self, track: Track, det: Detection, stamp: float) -> None:
        dt = stamp - track.last_seen
        if dt > 1e-3:
            vx = (det.bbox.cx - track.bbox.cx) / dt
            vy = (det.bbox.cy - track.bbox.cy) / dt
            cap = self.cfg.max_velocity_px_s
            # Smooth, so a single noisy detection does not dominate the estimate.
            track.vx = 0.5 * track.vx + 0.5 * max(-cap, min(cap, vx))
            track.vy = 0.5 * track.vy + 0.5 * max(-cap, min(cap, vy))

        track.bbox = det.bbox
        track.score = det.score
        track.keypoints = det.keypoints
        track.stamp = stamp
        track.last_seen = stamp
        track.hits += 1
        track.misses = 0
        if track.hits >= self.cfg.min_hits:
            track.confirmed = True

    def _spawn(self, det: Detection, stamp: float) -> None:
        track = Track(
            track_id=self._next_id,
            bbox=det.bbox,
            stamp=stamp,
            score=det.score,
            keypoints=det.keypoints,
            first_seen=stamp,
            last_seen=stamp,
            confirmed=self.cfg.min_hits <= 1,
        )
        self._next_id += 1
        self.tracks.append(track)

    # -- queries -----------------------------------------------------------

    def get(self, track_id: int) -> Track | None:
        for t in self.tracks:
            if t.track_id == track_id:
                return t
        return None

    def confirmed_tracks(self) -> list[Track]:
        return [t for t in self.tracks if t.confirmed]

    def visible_tracks(self, stamp: float, tolerance_s: float = 1e-6) -> list[Track]:
        """Tracks matched to a detection on the most recent frame."""
        return [
            t for t in self.tracks if t.confirmed and (stamp - t.last_seen) <= tolerance_s
        ]
