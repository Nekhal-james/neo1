"""The palm check has to survive the trip to the browser.

It is only useful if it arrives: through PerceptionLink.view(), and through the
asdict() the 4 Hz state broadcast uses -- which silently drops anything that is
not a real field.
"""

from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

import pytest

from neo_perception.types import GestureKind, PalmCheck, PerceptionResult
from neo_webapp.perception_link import PerceptionLink


def _link(result: PerceptionResult) -> PerceptionLink:
    link = PerceptionLink.__new__(PerceptionLink)
    link._last_gesture = "none"
    link.runner = SimpleNamespace(
        result=result,
        pipeline=SimpleNamespace(
            identify_pending=False,
            identify_seq=0,
            last_identify_error="",
            detector=SimpleNamespace(name="mock"),
            engagement=SimpleNamespace(
                last_release_reason="",
                cfg=SimpleNamespace(engage_gesture=GestureKind.OPEN_PALM),
            ),
        ),
    )
    return link


def test_the_palm_check_reaches_the_panel():
    check = PalmCheck(
        3,
        verdict=GestureKind.RAISED_HAND,
        reason="right elbow not visible (up close it is often below the frame)",
        arm="right",
        lift=0.9,
        elbow_score=0.12,
        wrist_score=0.8,
    )
    result = PerceptionResult(stamp=1.0, width=640, height=480, palm_check=check,
                              hold_progress=0.25)
    view = _link(result).view(running=True)

    assert view.palm.verdict == "raised_hand"
    assert "elbow" in view.palm.reason
    assert view.hold_progress == pytest.approx(0.25)

    body = asdict(view)
    assert body["palm"]["elbow_score"] == pytest.approx(0.12)
    assert body["palm"]["track_id"] == 3


def test_no_check_is_null_rather_than_an_error():
    view = _link(PerceptionResult(stamp=1.0, width=640, height=480)).view(running=True)
    assert view.palm is None
    assert asdict(view)["palm"] is None
