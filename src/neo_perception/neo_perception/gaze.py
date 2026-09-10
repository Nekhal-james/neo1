"""Turning "where is the person in the image" into "how should the head move".

The camera is mounted on the head, so this is a closed loop: pan right and the
person moves left in frame. That makes proportional control on the *image* error
the natural formulation -- no camera intrinsics, no distance estimate, no
kinematics. Centring a face does not need any of them.

Output is normalised rate in [-1, 1] per axis, deliberately the same convention as
the admin panel's virtual joystick, so gaze and manual control feed the head
through one interface and one arbiter.
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import AttentionTarget, EngagementDecision, Track


@dataclass
class GazeConfig:
    kp: float = 1.1
    """Proportional gain on normalised image error."""

    deadband: float = 0.08
    """Error below this is ignored.

    Without it the head hunts forever around centre, which reads as a nervous
    twitch and grinds the servos for no benefit.
    """

    max_rate: float = 1.0

    face_bias: float = 0.0
    """Vertical aim offset in normalised units, positive aims higher.

    Useful once the real camera is mounted: if it sits below eye level the robot
    should still aim at the face, not the chest.
    """

    engaged_gain: float = 1.0
    scanning_gain: float = 0.35
    """Casual glances at unengaged people are slower and softer than tracking the
    person who actually asked for attention."""


@dataclass(frozen=True)
class GazeCommand:
    pan: float = 0.0
    tilt: float = 0.0
    active: bool = False


class GazeMapper:
    def __init__(self, config: GazeConfig | None = None) -> None:
        self.cfg = config or GazeConfig()

    def aim_point(self, track: Track) -> tuple[float, float]:
        """Where on this person to look, in pixels.

        Prefers the face keypoints; falls back to the upper third of the box,
        which approximates head height far better than the box centre when the
        detector has no pose or the face is turned away.
        """
        if track.keypoints is not None:
            face = track.keypoints.face_anchor()
            if face is not None:
                return face
        if track.person_bbox is not None:
            # `bbox` is already the head, so its centre is the aim point. The
            # upper-third rule below exists only to find a head inside a
            # whole-body box, and applying it here would aim at the forehead.
            return (track.bbox.cx, track.bbox.cy)
        return (track.bbox.cx, track.bbox.y1 + track.bbox.height / 3.0)

    def to_attention(
        self, decision: EngagementDecision, frame_width: int, frame_height: int
    ) -> AttentionTarget:
        if decision.target is None or frame_width <= 0 or frame_height <= 0:
            return AttentionTarget(
                person_present=decision.person_present,
                engaged=decision.engaged,
                state=decision.state,
                track_id=decision.track_id,
                confidence=decision.confidence,
            )

        px, py = self.aim_point(decision.target)
        # Normalise to [-1, 1]; flip y so positive is up, matching the joystick.
        x = (px / frame_width) * 2.0 - 1.0
        y = 1.0 - (py / frame_height) * 2.0 + self.cfg.face_bias
        return AttentionTarget(
            x=_clamp(x, -1.0, 1.0),
            y=_clamp(y, -1.0, 1.0),
            confidence=decision.confidence,
            track_id=decision.track_id,
            person_present=decision.person_present,
            engaged=decision.engaged,
            state=decision.state,
        )

    def to_command(self, target: AttentionTarget) -> GazeCommand:
        """Proportional control on image error, with a deadband."""
        if target.confidence <= 0.0:
            # Nothing to aim at -- including while SUSPENDED, where holding the
            # last position is right and drifting toward centre is not.
            return GazeCommand()

        gain = self.cfg.engaged_gain if target.engaged else self.cfg.scanning_gain
        pan = _deadband(target.x, self.cfg.deadband) * self.cfg.kp * gain
        # Positive tilt is up, and the head's tilt axis is also positive up, so
        # the error passes through without a sign flip.
        tilt = _deadband(target.y, self.cfg.deadband) * self.cfg.kp * gain

        pan = _clamp(pan, -self.cfg.max_rate, self.cfg.max_rate)
        tilt = _clamp(tilt, -self.cfg.max_rate, self.cfg.max_rate)
        return GazeCommand(pan=pan, tilt=tilt, active=bool(pan or tilt))


def _deadband(value: float, width: float) -> float:
    """Zero inside the band, and continuous at its edge.

    Subtracting the band rather than passing the raw value through avoids a jump
    from 0 to `width` the moment the threshold is crossed.
    """
    if abs(value) <= width:
        return 0.0
    return value - width if value > 0 else value + width


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))
