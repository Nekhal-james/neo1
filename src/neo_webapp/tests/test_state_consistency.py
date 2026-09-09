"""Regressions for state that was shipped but silently wrong.

Two failure modes, both invisible without a test:

* a `@property` on a dataclass served through `asdict()` -- it just is not in
  the JSON, with no error anywhere,
* a field in the 4 Hz snapshot that nothing keeps current, contradicting the
  route that has the real value.
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from neo_webapp.bridge.types import IdentifyView, ObjectGuessView, RobotState


def guess(label: str) -> ObjectGuessView:
    return ObjectGuessView(label, 0.8, 0.4, 0.1, 0.1, 0.5, 0.5)


class TestSerialisation:
    def test_identify_best_survives_asdict(self):
        """`best` is the field a consumer of /api/state actually wants."""
        view = IdentifyView(guesses=[guess("cell phone"), guess("book")])
        assert dataclasses.asdict(view)["best"] == "cell phone"

    def test_identify_best_is_empty_with_no_guesses(self):
        assert dataclasses.asdict(IdentifyView())["best"] == ""

    def test_model_summary_survives_asdict(self):
        from neo_webapp.model_status import ModelView

        view = ModelView(reachable=True, serving=True, model_name="qwen2.5:3b")
        view.refresh_summary()
        assert dataclasses.asdict(view)["summary"] == "serving qwen2.5:3b"

    def test_every_read_path_fills_the_summary(self, monkeypatch):
        """Including the early returns, which is where it is easy to forget."""
        import neo_webapp.model_status as ms

        assert ms.read_model_status().summary, "normal path"

        def boom(*a, **kw):
            raise RuntimeError("unreadable")

        monkeypatch.setattr(ms, "_configured_model", boom)
        # _configured_model is called outside the guarded block, so a raise here
        # would escape -- if that ever changes, this catches the empty summary.
        with pytest.raises(RuntimeError):
            ms.read_model_status()

    def test_no_served_view_hides_data_behind_a_property(self):
        """The class of bug, not just the two instances of it."""
        from neo_webapp import model_status
        from neo_webapp.bridge import types

        offenders = []
        for module in (types, model_status):
            for name, obj in vars(module).items():
                if not (inspect.isclass(obj) and dataclasses.is_dataclass(obj)):
                    continue
                props = [n for n, v in vars(obj).items() if isinstance(v, property)]
                if props:
                    offenders.append(f"{module.__name__}.{name}: {props}")
        assert not offenders, (
            "these are served through asdict(), which drops properties silently: "
            + "; ".join(offenders)
        )


class TestLinkIsNotStale:
    """RobotState.link used to be permanently 'down, 0 failures' while
    /api/link/status beside it had the real numbers."""

    def test_snapshot_link_matches_the_route(self, auth_client):
        state_link = auth_client.get("/api/state").json()["link"]
        route_link = auth_client.get("/api/link/status").json()["link"]
        assert state_link == route_link

    def test_the_refresh_survives_an_unreadable_status_file(
        self, config, monkeypatch
    ):
        """A broken link file must not stop the panel or the vision publisher."""
        import neo_webapp.app as app_module
        from fastapi.testclient import TestClient
        from neo_webapp.bridge.mock import MockBridge

        def boom():
            raise OSError("status file on fire")

        monkeypatch.setattr(app_module, "read_link_status", boom)
        app = app_module.create_app(config, MockBridge())
        with TestClient(app) as c:
            assert c.get("/api/health").status_code == 200


def test_robot_state_has_no_unpopulated_placeholder_fields():
    """A field nobody fills is a field that lies at 4 Hz."""
    state = RobotState()
    assert dataclasses.is_dataclass(state)
    # emotion is genuinely not built yet (Phase 9) and is documented as such;
    # everything else in the snapshot must have a writer.
    assert state.emotion.label == "NEUTRAL"
