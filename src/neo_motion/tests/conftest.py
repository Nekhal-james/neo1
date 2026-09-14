"""Shared test fixtures for neo_motion."""

from __future__ import annotations

import contextlib
import logging


@contextlib.contextmanager
def captured_log(logger_name: str, level: int = logging.WARNING):
    """Collect log records from one logger directly, bypassing caplog.

    Under colcon's system pytest 7.4.4 plus the ament/launch-testing plugins,
    two LogCaptureHandler instances end up attached to the root logger at
    once; caplog.set_level() only configures one of them, and caplog.text
    reads from the other, which never received the record -- confirmed by the
    message landing on stderr via logging's lastResort fallback instead of
    being captured. caplog works fine under the venv's plain pytest, where
    this repo's other caplog-based tests pass, so the fix is not "use caplog
    differently" but "don't depend on caplog here" -- attaching a plain
    handler straight to the logger under test sidesteps the plugin
    interaction entirely.
    """
    records: list[logging.LogRecord] = []
    handler = logging.Handler(level=level)
    handler.emit = records.append  # type: ignore[method-assign]
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
