"""neo-servo-check's --record-neutral / --record-speed: the arithmetic-and-YAML
half of calibrating a continuous-rotation servo. No hardware involved -- these
flags never touch PWM or I2C, so they run the same on a laptop as on the Pi.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from neo_motion.config import load_raw
from neo_motion.scripts.servo_check import main


def test_record_neutral_writes_the_pulse(tmp_path, capsys):
    path = tmp_path / "motion.local.yaml"
    rc = main(["--record-neutral", "pan", "1500", "--config", str(path)])
    assert rc == 0
    assert load_raw(path)["pan"] == {"neutral_us": 1500.0}
    assert "neutral_us = 1500" in capsys.readouterr().out


def test_record_neutral_rejects_an_unknown_axis(tmp_path, capsys):
    path = tmp_path / "motion.local.yaml"
    rc = main(["--record-neutral", "roll", "1500", "--config", str(path)])
    assert rc == 2
    assert not path.exists()
    assert "FAIL" in capsys.readouterr().out


def test_record_speed_needs_a_neutral_first(tmp_path, capsys):
    path = tmp_path / "motion.local.yaml"
    rc = main([
        "--record-speed", "pan", "1650",
        "--turns", "6.5", "--seconds", "40",
        "--config", str(path),
    ])
    assert rc == 2
    assert "record-neutral" in capsys.readouterr().out
    assert load_raw(path) == {}


def test_record_speed_computes_deg_per_second_and_picks_the_right_side(tmp_path):
    path = tmp_path / "motion.local.yaml"
    main(["--record-neutral", "pan", "1500", "--config", str(path)])

    # Above neutral: 6.5 turns in 40 s.
    rc = main([
        "--record-speed", "pan", "1650",
        "--turns", "6.5", "--seconds", "40",
        "--config", str(path),
    ])
    assert rc == 0
    pan = load_raw(path)["pan"]
    assert pan["above_us"] == 1650.0
    assert pan["above_deg_s"] == pytest.approx(6.5 * 360.0 / 40.0)

    # Below neutral: a different measured speed, on the other side.
    rc = main([
        "--record-speed", "pan", "1350",
        "--turns", "9.0", "--seconds", "40",
        "--config", str(path),
    ])
    assert rc == 0
    pan = load_raw(path)["pan"]
    assert pan["below_us"] == 1350.0
    assert pan["below_deg_s"] == pytest.approx(9.0 * 360.0 / 40.0)
    # Recording the second side must not disturb the first.
    assert pan["above_us"] == 1650.0
    assert pan["neutral_us"] == 1500.0


def test_record_speed_refuses_the_neutral_pulse_itself(tmp_path, capsys):
    path = tmp_path / "motion.local.yaml"
    main(["--record-neutral", "pan", "1500", "--config", str(path)])
    rc = main([
        "--record-speed", "pan", "1500",
        "--turns", "5", "--seconds", "40",
        "--config", str(path),
    ])
    assert rc == 2
    assert "clear of it" in capsys.readouterr().out
    assert "above_us" not in load_raw(path)["pan"]


def test_record_speed_needs_turns_and_seconds(tmp_path, capsys):
    path = tmp_path / "motion.local.yaml"
    main(["--record-neutral", "pan", "1500", "--config", str(path)])
    rc = main(["--record-speed", "pan", "1650", "--config", str(path)])
    assert rc == 2
    assert "--turns" in capsys.readouterr().out
