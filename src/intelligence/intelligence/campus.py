"""The campus dataset: the three YAML files, their validation, and saving them.

Vectorless by design (CLAUDE.md): the data is structured records, and retrieval
(`retrieval.py`) is lookup and filtering over them. Nothing here embeds text.

Three files under `rag.data_dir`, all optional, so a dataset can start at one
room:

    rooms.yaml      one entry per place people ask for
    graph.yaml      a walkable graph, for directions from reception
    coverage.yaml   which blocks/floors have been surveyed

The admin panel edits these as text and saves the operator's text verbatim, so
comments survive. Validation is what keeps that safe: an unknown field is an
error rather than a warning, because `alias:` for `aliases:` would otherwise
drop every alias silently and the only symptom would be Neo not finding a room.
"""

from __future__ import annotations

import difflib
import heapq
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

FILES = ("rooms", "graph", "coverage")

ROOM_TYPES = ("classroom", "lab", "office", "facility")
COVERAGE_STATUSES = ("complete", "partial", "not_surveyed")

ROOM_FIELDS = (
    "code", "name", "type", "block", "floor", "wing", "department",
    "node", "aliases", "landmarks", "directions", "notes",
)
REQUIRED_ROOM_FIELDS = ("code", "name", "block", "floor")
NODE_FIELDS = ("id", "kind", "block", "floor")
EDGE_FIELDS = ("from", "to", "instruction", "reverse_instruction", "distance_m", "kind")

ORIGIN_NODE = "reception"
"""Where Neo stands. neo_msgs/srv/QueryKb.srv makes the same default."""

MAX_FILE_BYTES = 1024 * 1024
"""Five hundred rooms is well under 200 KB; a megabyte is a mistake, not a campus."""

GENERIC_WORDS = frozenset({
    "lab", "labs", "room", "rooms", "class", "classroom", "classrooms", "office",
    "offices", "block", "floor", "hall", "building", "department", "dept",
    "the", "a", "an", "where", "is", "near",
})
"""Aliases that match nearly every question if used on their own."""


# -- the dataset -----------------------------------------------------------


@dataclass
class Room:
    code: str
    name: str
    block: str
    floor: int
    type: str = ""
    wing: str = ""
    department: str = ""
    node: str = ""
    aliases: list[str] = field(default_factory=list)
    landmarks: list[str] = field(default_factory=list)
    directions: str = ""
    notes: str = ""


@dataclass
class Edge:
    src: str
    dst: str
    instruction: str
    reverse_instruction: str = ""
    distance_m: float = 1.0
    kind: str = ""


@dataclass
class BlockCoverage:
    block: str
    status: str
    floors: list[int] = field(default_factory=list)


@dataclass
class Dataset:
    rooms: list[Room] = field(default_factory=list)
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    coverage: dict[str, BlockCoverage] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.rooms and not self.coverage

    def blocks(self) -> list[str]:
        """Every block named anywhere, in a stable order."""
        seen = {r.block for r in self.rooms} | set(self.coverage)
        return sorted(seen, key=lambda b: (len(b), b))

    def route(self, destination: str, origin: str = ORIGIN_NODE) -> list[str] | None:
        """Instructions from `origin` to `destination`, shortest by distance.

        Edges are one-way unless they carry `reverse_instruction`: the text of an
        edge describes walking it in one direction, and read backwards ("take the
        stairs up one floor") it is wrong.
        """
        if origin not in self.nodes or destination not in self.nodes:
            return None
        if origin == destination:
            return []
        adjacency: dict[str, list[tuple[float, str, str]]] = {}
        for e in self.edges:
            adjacency.setdefault(e.src, []).append((e.distance_m, e.dst, e.instruction))
            if e.reverse_instruction:
                adjacency.setdefault(e.dst, []).append(
                    (e.distance_m, e.src, e.reverse_instruction)
                )
        best: dict[str, float] = {origin: 0.0}
        queue: list[tuple[float, str, list[str]]] = [(0.0, origin, [])]
        while queue:
            dist, node, steps = heapq.heappop(queue)
            if node == destination:
                return steps
            if dist > best.get(node, float("inf")):
                continue
            for length, nxt, text in adjacency.get(node, []):
                nd = dist + length
                if nd < best.get(nxt, float("inf")):
                    best[nxt] = nd
                    heapq.heappush(queue, (nd, nxt, steps + [text]))
        return None


# -- validation ------------------------------------------------------------


@dataclass
class Issue:
    file: str
    severity: str  # "error" | "warning"
    where: str
    message: str
    line: int | None = None


@dataclass
class Validation:
    dataset: Dataset
    issues: list[Issue]

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors


def summary(ds: Dataset) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for r in ds.rooms:
        counts[r.block] = counts.get(r.block, 0) + 1
    blocks = []
    for b in ds.blocks():
        cov = ds.coverage.get(b)
        blocks.append({
            "block": b,
            "status": cov.status if cov else "unlisted",
            "floors": cov.floors if cov else sorted({r.floor for r in ds.rooms if r.block == b}),
            "rooms": counts.get(b, 0),
        })
    return {
        "rooms": len(ds.rooms),
        "nodes": len(ds.nodes),
        "edges": len(ds.edges),
        "blocks": blocks,
    }


def validate(texts: dict[str, str]) -> Validation:
    """Parse and cross-check the three files, given as text.

    Always returns a dataset built from whatever was valid, so a caller can
    still answer from a file with one bad entry; saving refuses on any error.
    """
    issues: list[Issue] = []
    ds = Dataset()
    lines: dict[str, dict[int, int]] = {}

    raw: dict[str, Any] = {}
    for name in FILES:
        text = texts.get(name, "") or ""
        if len(text.encode("utf-8")) > MAX_FILE_BYTES:
            issues.append(Issue(name, "error", name, f"file is over {MAX_FILE_BYTES // 1024} KB"))
            continue
        try:
            raw[name] = yaml.safe_load(text) if text.strip() else None
            lines[name] = _item_lines(text)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            problem = getattr(exc, "problem", None) or str(exc)
            issues.append(Issue(
                name, "error", name, f"not valid YAML: {problem}",
                line=(mark.line + 1) if mark is not None else None,
            ))

    _parse_rooms(raw.get("rooms"), ds, issues, lines.get("rooms", {}))
    _parse_graph(raw.get("graph"), ds, issues, lines.get("graph", {}))
    _parse_coverage(raw.get("coverage"), ds, issues)
    _cross_check(ds, issues)
    return Validation(dataset=ds, issues=issues)


def _item_lines(text: str) -> dict[int, int]:
    """Line number of each top-level list item, by its index.

    Good enough to point an operator at "line 42" for `rooms[7]` without a
    position-tracking YAML loader. Only rooms.yaml is a top-level list, so only
    its issues carry a line.
    """
    found: dict[int, int] = {}
    index = 0
    for number, line in enumerate(text.splitlines(), start=1):
        if re.match(r"^-(\s|$)", line):
            found[index] = number
            index += 1
    return found


def _unknown_fields(entry: dict, allowed: tuple[str, ...], file: str, where: str,
                    issues: list[Issue], line: int | None) -> None:
    for key in entry:
        if key not in allowed:
            close = difflib.get_close_matches(str(key), allowed, n=1)
            hint = f" -- did you mean '{close[0]}'?" if close else ""
            issues.append(Issue(
                file, "error", where,
                f"unknown field '{key}'{hint} (allowed: {', '.join(allowed)})", line,
            ))


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _string_list(value: Any, file: str, where: str, name: str,
                 issues: list[Issue], line: int | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        issues.append(Issue(file, "error", where,
                            f"'{name}' must be a list, like [\"{value}\"]", line))
        return []
    if not isinstance(value, list):
        issues.append(Issue(file, "error", where, f"'{name}' must be a list", line))
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _parse_rooms(data: Any, ds: Dataset, issues: list[Issue], lines: dict[int, int]) -> None:
    if data is None:
        return
    if not isinstance(data, list):
        issues.append(Issue("rooms", "error", "rooms",
                            "rooms.yaml must be a list of rooms, each starting with '- code:'"))
        return
    codes: dict[str, str] = {}
    for i, entry in enumerate(data):
        line = lines.get(i)
        where = f"rooms[{i}]"
        if not isinstance(entry, dict):
            issues.append(Issue("rooms", "error", where, "each room must be a set of fields", line))
            continue
        code = _text(entry.get("code"))
        if code:
            where = f"rooms[{i}] ({code})"
        _unknown_fields(entry, ROOM_FIELDS, "rooms", where, issues, line)

        missing = [f for f in REQUIRED_ROOM_FIELDS if _text(entry.get(f)) == ""]
        if missing:
            issues.append(Issue("rooms", "error", where,
                                f"missing required field(s): {', '.join(missing)}", line))
            continue

        floor = _int(entry.get("floor"))
        if floor is None:
            issues.append(Issue("rooms", "error", where,
                                "'floor' must be a whole number (0 is the ground floor)", line))
            continue

        block = _text(entry.get("block"))
        if not _block_ok(block, "rooms", where, issues, line):
            continue

        room_type = _text(entry.get("type")).lower()
        if room_type and room_type not in ROOM_TYPES:
            issues.append(Issue("rooms", "error", where,
                                f"'type' must be one of {', '.join(ROOM_TYPES)}", line))
            continue

        key = code.upper()
        if key in codes:
            issues.append(Issue("rooms", "error", where,
                                f"code '{code}' is already used by {codes[key]}", line))
            continue
        codes[key] = where

        room = Room(
            code=code,
            name=_text(entry.get("name")),
            block=block,
            floor=floor,
            type=room_type,
            wing=_text(entry.get("wing")),
            department=_text(entry.get("department")),
            node=_text(entry.get("node")),
            aliases=_string_list(entry.get("aliases"), "rooms", where, "aliases", issues, line),
            landmarks=_string_list(entry.get("landmarks"), "rooms", where, "landmarks", issues, line),
            directions=_text(entry.get("directions")),
            notes=_text(entry.get("notes")),
        )
        ds.rooms.append(room)


def _block_ok(block: str, file: str, where: str, issues: list[Issue], line: int | None) -> bool:
    if re.search(r"\bblock\b", block, re.IGNORECASE):
        bare = re.sub(r"\bblock\b", "", block, flags=re.IGNORECASE).strip()
        issues.append(Issue(file, "error", where,
                            f"write the block as '{bare}', not '{block}' -- Neo adds the word 'block' itself",
                            line))
        return False
    return True


def _parse_graph(data: Any, ds: Dataset, issues: list[Issue], lines: dict[int, int]) -> None:
    if data is None:
        return
    if not isinstance(data, dict):
        issues.append(Issue("graph", "error", "graph", "graph.yaml must have 'nodes:' and 'edges:' sections"))
        return
    for key in data:
        if key not in ("nodes", "edges"):
            issues.append(Issue("graph", "error", "graph",
                                f"unknown section '{key}' (allowed: nodes, edges)"))

    nodes = data.get("nodes") or []
    if not isinstance(nodes, list):
        issues.append(Issue("graph", "error", "nodes", "'nodes' must be a list"))
        nodes = []
    for i, entry in enumerate(nodes):
        where = f"nodes[{i}]"
        if not isinstance(entry, dict) or not _text(entry.get("id")):
            issues.append(Issue("graph", "error", where, "each node needs an 'id'"))
            continue
        node_id = _text(entry["id"])
        where = f"nodes[{i}] ({node_id})"
        _unknown_fields(entry, NODE_FIELDS, "graph", where, issues, None)
        if node_id in ds.nodes:
            issues.append(Issue("graph", "error", where, f"node id '{node_id}' is used twice"))
            continue
        ds.nodes[node_id] = dict(entry)

    edges = data.get("edges") or []
    if not isinstance(edges, list):
        issues.append(Issue("graph", "error", "edges", "'edges' must be a list"))
        edges = []
    for i, entry in enumerate(edges):
        where = f"edges[{i}]"
        if not isinstance(entry, dict):
            issues.append(Issue("graph", "error", where, "each edge must be a set of fields"))
            continue
        src, dst = _text(entry.get("from")), _text(entry.get("to"))
        if src and dst:
            where = f"edges[{i}] ({src} -> {dst})"
        _unknown_fields(entry, EDGE_FIELDS, "graph", where, issues, None)
        if not src or not dst or not _text(entry.get("instruction")):
            issues.append(Issue("graph", "error", where, "each edge needs 'from', 'to' and 'instruction'"))
            continue
        unknown = [n for n in (src, dst) if n not in ds.nodes]
        if unknown:
            issues.append(Issue("graph", "error", where,
                                f"unknown node(s): {', '.join(unknown)} -- add them under 'nodes:'"))
            continue
        distance = entry.get("distance_m", 1.0)
        if isinstance(distance, bool) or not isinstance(distance, (int, float)) or distance < 0:
            issues.append(Issue("graph", "error", where, "'distance_m' must be a positive number"))
            continue
        ds.edges.append(Edge(
            src=src, dst=dst,
            instruction=_text(entry["instruction"]),
            reverse_instruction=_text(entry.get("reverse_instruction")),
            distance_m=float(distance),
            kind=_text(entry.get("kind")),
        ))


def _parse_coverage(data: Any, ds: Dataset, issues: list[Issue]) -> None:
    if data is None:
        return
    if not isinstance(data, dict) or set(data) - {"blocks"}:
        issues.append(Issue("coverage", "error", "coverage",
                            "coverage.yaml must have a single 'blocks:' section"))
        return
    blocks = data.get("blocks") or {}
    if not isinstance(blocks, dict):
        issues.append(Issue("coverage", "error", "blocks",
                            "'blocks' must map each block to its status, like  A: {status: complete, floors: [0, 1]}"))
        return
    for raw_block, entry in blocks.items():
        block = _text(raw_block)
        where = f"blocks.{block}"
        if isinstance(raw_block, bool):
            issues.append(Issue("coverage", "error", where,
                                "block name was read as true/false -- put it in quotes"))
            continue
        if not _block_ok(block, "coverage", where, issues, None):
            continue
        if not isinstance(entry, dict):
            issues.append(Issue("coverage", "error", where,
                                "give each block a status, like  {status: partial, floors: [2]}"))
            continue
        _unknown_fields(entry, ("status", "floors"), "coverage", where, issues, None)
        status = _text(entry.get("status")).lower()
        if status not in COVERAGE_STATUSES:
            issues.append(Issue("coverage", "error", where,
                                f"'status' must be one of {', '.join(COVERAGE_STATUSES)}"))
            continue
        floors_raw = entry.get("floors") or []
        floors = [_int(f) for f in floors_raw] if isinstance(floors_raw, list) else [None]
        if any(f is None for f in floors):
            issues.append(Issue("coverage", "error", where, "'floors' must be a list of whole numbers"))
            continue
        if status != "not_surveyed" and not floors:
            issues.append(Issue("coverage", "warning", where,
                                "no floors listed -- say which floors are done, like floors: [0, 1]"))
        ds.coverage[block] = BlockCoverage(block=block, status=status, floors=sorted(set(floors)))


def _cross_check(ds: Dataset, issues: list[Issue]) -> None:
    from .retrieval import normalize

    owners: dict[str, list[str]] = {}
    for room in ds.rooms:
        where = f"rooms ({room.code})"
        cov = ds.coverage.get(room.block)
        if ds.coverage and cov is None:
            issues.append(Issue("rooms", "warning", where,
                                f"block {room.block} is not in coverage.yaml, so Neo cannot tell a "
                                f"missing room there from a block it has not learned"))
        elif cov is not None and cov.status == "not_surveyed":
            issues.append(Issue("coverage", "warning", f"blocks.{room.block}",
                                f"marked not_surveyed but has rooms ({room.code}) -- mark it partial"))
        elif cov is not None and cov.floors and room.floor not in cov.floors:
            issues.append(Issue("coverage", "warning", f"blocks.{room.block}",
                                f"{room.code} is on floor {room.floor}, which is not in this block's floors"))

        if room.node and ds.nodes and room.node not in ds.nodes:
            issues.append(Issue("rooms", "error", where,
                                f"node '{room.node}' is not in graph.yaml"))
        elif (room.node and ORIGIN_NODE in ds.nodes and not room.directions
              and ds.route(room.node) is None):
            issues.append(Issue("graph", "warning", where,
                                f"no route from '{ORIGIN_NODE}' to '{room.node}', so Neo has no directions for it"))

        keys = [("code", room.code), ("name", room.name)] + [("alias", a) for a in room.aliases]
        for kind, value in keys:
            norm = normalize(value)
            if not norm:
                continue
            owners.setdefault(norm, []).append(f"{room.code} ({kind})")
            if kind == "alias" and (norm in GENERIC_WORDS or len(norm) <= 2):
                issues.append(Issue("rooms", "warning", where,
                                    f"alias '{value}' is too general and will match unrelated questions"))

    for norm, who in owners.items():
        rooms = {w.split(" (")[0] for w in who}
        if len(rooms) < 2:
            continue
        if any(w.endswith("(code)") for w in who):
            issues.append(Issue("rooms", "error", "rooms",
                                f"'{norm}' is a room code and also names another room: {', '.join(who)}"))
        else:
            issues.append(Issue("rooms", "warning", "rooms",
                                f"'{norm}' matches several rooms ({', '.join(who)}) -- Neo will ask which one"))

    if ds.nodes and ORIGIN_NODE not in ds.nodes:
        issues.append(Issue("graph", "warning", "nodes",
                            f"no node with id '{ORIGIN_NODE}' -- directions start from there"))


# -- files -----------------------------------------------------------------


def file_path(data_dir: Path, name: str) -> Path:
    if name not in FILES:
        raise ValueError(f"unknown campus file '{name}' (expected one of {', '.join(FILES)})")
    return data_dir / f"{name}.yaml"


def read_texts(data_dir: Path) -> dict[str, str]:
    out = {}
    for name in FILES:
        p = file_path(data_dir, name)
        out[name] = p.read_text(encoding="utf-8") if p.exists() else ""
    return out


def modified_ns(data_dir: Path, name: str) -> int:
    p = file_path(data_dir, name)
    return p.stat().st_mtime_ns if p.exists() else 0


_cache: dict[Path, tuple[tuple[int, ...], Dataset]] = {}


def load_dataset(data_dir: Path) -> Dataset:
    """The dataset on disk, re-parsed only when a file changed.

    Read on every question rather than at startup, so a room saved in the panel
    is answerable by the very next question with no restart (plan 7.3).
    """
    stamp = tuple(modified_ns(data_dir, n) for n in FILES)
    cached = _cache.get(data_dir)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    ds = validate(read_texts(data_dir)).dataset
    _cache[data_dir] = (stamp, ds)
    return ds


def save_text(data_dir: Path, name: str, text: str, backup_dir: Path, keep: int = 30) -> Path | None:
    """Atomically replace one file, keeping the previous version as a backup.

    The dataset is the project's most valuable asset and it lives on an SD card:
    a torn write or a bad paste must never be the only copy.
    """
    target = file_path(data_dir, name)
    data_dir.mkdir(parents=True, exist_ok=True)
    backup = None
    if target.exists():
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = backup_dir / f"{name}-{stamp}-{time.monotonic_ns() % 1_000_000:06d}.yaml"
        backup.write_bytes(target.read_bytes())
        old = sorted(backup_dir.glob(f"{name}-*.yaml"))
        for stale in old[:-keep]:
            stale.unlink(missing_ok=True)

    fd, tmp = tempfile.mkstemp(dir=data_dir, prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text if text.endswith("\n") or not text else text + "\n")
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    _cache.pop(data_dir, None)
    return backup


# -- samples ---------------------------------------------------------------
#
# Served to the panel as the format reference, and validated by the tests, so
# the example an operator copies can never be one Neo would reject.

SAMPLE_ROOMS = """\
# One entry per place people ask for: classrooms, labs, offices, and facilities
# like the library, canteen or washrooms. Required: code, name, block, floor.

- code: CS-204                  # exactly as signposted on the door
  name: Computer Science Lab 2  # the official name
  type: lab                     # classroom | lab | office | facility
  block: B                      # just the letter or number -- not "B block"
  floor: 2                      # 0 is the ground floor
  wing: East
  department: CSE
  node: b2_lab_corridor         # optional: nearest node in graph.yaml
  aliases: ["cs lab 2", "second cs lab", "programming lab"]
  landmarks: ["opposite the lift", "next to the staff room"]
  notes: "Open 9 am to 5 pm on weekdays."

- code: LIB
  name: Central Library
  type: facility
  block: A
  floor: 1
  aliases: ["library", "reading room"]
  landmarks: ["above the main entrance"]
  directions: "Take the stairs on your left up one floor; the library is straight ahead."
  notes: "Open 8 am to 8 pm, Monday to Saturday."
"""

SAMPLE_GRAPH = """\
# Optional. The campus as a walkable graph, so Neo can give directions from
# reception. Rooms point at a node with 'node:'. Edges are one-way unless they
# have 'reverse_instruction'. A room's own 'directions:' text wins over this.

nodes:
  - id: reception               # where Neo stands -- directions start here
    kind: entrance
    block: A
    floor: 0
  - id: b_stairs_ground
    kind: stairs
    block: B
    floor: 0
  - id: b2_lab_corridor
    kind: junction
    block: B
    floor: 2

edges:
  - from: reception
    to: b_stairs_ground
    distance_m: 40
    instruction: "go out through the back door and cross the courtyard to B block"
    reverse_instruction: "cross the courtyard back to the main building"
  - from: b_stairs_ground
    to: b2_lab_corridor
    kind: stairs
    distance_m: 15
    instruction: "take the stairs up two floors and turn right"
"""

SAMPLE_COVERAGE = """\
# What has been surveyed, so Neo can be honest about what it does not know.
#   complete      every room on the listed floors is in rooms.yaml
#   partial       some rooms entered -- a miss here might still exist
#   not_surveyed  nothing entered yet: "I haven't learned C block yet"

blocks:
  A: {status: complete, floors: [0, 1]}
  B: {status: partial, floors: [2]}
  C: {status: not_surveyed}
"""

SAMPLES = {"rooms": SAMPLE_ROOMS, "graph": SAMPLE_GRAPH, "coverage": SAMPLE_COVERAGE}
