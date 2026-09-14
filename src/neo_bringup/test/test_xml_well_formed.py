"""Every XML file the robot's tooling reads must parse.

Two invalid files have reached the Pi so far, both from the same trap: XML
forbids a double hyphen anywhere inside a comment. One was the CycloneDDS
config, which would have stopped every ROS node; the other was
neo_motion/package.xml, which stopped `setup-system.sh` at `rosdep install`,
because rosdep reads every package.xml under src/ -- COLCON_IGNORE or not.
Neither broke a single test, because nothing parsed them. This does.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
XML_FILES = sorted(
    list((REPO_ROOT / "src").glob("*/package.xml"))
    + list((REPO_ROOT / "scripts" / "pi" / "files").glob("*.xml"))
)


def test_the_files_are_found():
    """An empty list would pass every check below while checking nothing."""
    assert len([p for p in XML_FILES if p.name == "package.xml"]) >= 8
    assert any(p.name == "cyclonedds.xml" for p in XML_FILES)


@pytest.mark.parametrize("path", XML_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_parses(path):
    try:
        ET.parse(path)
    except ET.ParseError as exc:
        line_no = exc.position[0]
        line = path.read_text(encoding="utf-8").splitlines()[line_no - 1]
        hint = "  (a double hyphen inside an XML comment?)" if "--" in line else ""
        pytest.fail(f"{path.relative_to(REPO_ROOT)} line {line_no}: {exc}\n    {line.strip()}{hint}")
