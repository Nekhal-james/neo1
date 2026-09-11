"""Vectorless retrieval over the campus dataset.

Pure functions, no embeddings, no similarity search (CLAUDE.md). A question is
normalised, then matched as whole phrases against each room's code, name and
aliases; a block mention with no room match becomes a listing. What matched is
rendered two ways:

- `context()`: the "Campus directory" section of the model's system prompt,
  holding only the entries this question matched. The prompt tells the model
  those are the only campus facts it may use.
- `offline_reply()`: a spoken sentence built from the same entries, for when no
  model host is reachable -- the usual state, since the host is a daily-driver
  laptop. The info-desk role must work without it.

Normalisation is where most misses are won back, and it is applied to the data
and the question alike, so "CS-204", "cs204", "C S 204" and Vosk's "c s two oh
four" all meet at "cs 204".
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .campus import COVERAGE_STATUSES, Dataset, Room

CONTEXT_HEADER = "Campus directory"
"""The system prompt refers to this section by name; a test holds them together."""

MAX_HITS = 5
MAX_LISTING = 12

_UNITS = {
    "zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
}
_TEENS = {
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_NUMBER_WORDS = set(_UNITS) | set(_TEENS) | set(_TENS) | {"hundred"}

_LEAD_WORDS = {"room", "floor", "block", "lab", "number", "no", "level", "hall", "class"}
"""Words after which a lone number word is a number: "room one", "floor two"."""

_COMMON_SHORT = {
    "a", "an", "i", "is", "it", "the", "to", "in", "on", "at", "of", "for", "and",
    "or", "me", "my", "you", "how", "can", "get", "go", "do", "does", "any", "way",
    "be", "we", "us", "our", "its", "that", "this", "what", "who", "when", "with",
    "near", "from", "into", "out", "up", "down", "here", "there", "some", "all",
    "one", "which", "lab", "labs", "room", "no", "hall", "show", "tell", "take",
}

_TYPE_WORDS = {
    "lab": "lab", "labs": "lab", "laboratory": "lab", "laboratories": "lab",
    "classroom": "classroom", "classrooms": "classroom",
    "office": "office", "offices": "office",
    "facility": "facility", "facilities": "facility",
}

_ORDINALS = [
    "ground", "first", "second", "third", "fourth", "fifth",
    "sixth", "seventh", "eighth", "ninth", "tenth",
]


# -- normalisation ---------------------------------------------------------


def normalize(text: str) -> str:
    """Lowercase words and numbers, one space apart, spoken forms folded in."""
    t = str(text).lower()
    t = re.sub(r"([a-z])([0-9])", r"\1 \2", t)
    t = re.sub(r"([0-9])([a-z])", r"\1 \2", t)
    tokens = [tok for tok in re.split(r"[^a-z0-9]+", t) if tok]
    tokens = _fold_number_words(tokens)
    tokens = _join_spelled_letters(tokens)
    return " ".join(tokens)


def _fold_number_words(tokens: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(tokens):
        if tokens[i] not in _NUMBER_WORDS or tokens[i] == "hundred":
            out.append(tokens[i])
            i += 1
            continue
        j = i
        run: list[str] = []
        while j < len(tokens):
            tok = tokens[j]
            if tok in _NUMBER_WORDS:
                run.append(tok)
                j += 1
            elif (tok == "and" and run and run[-1] == "hundred"
                  and j + 1 < len(tokens) and tokens[j + 1] in _NUMBER_WORDS):
                j += 1
            else:
                break
        prev = out[-1] if out else ""
        lone_is_number = (
            run[0] != "oh"
            and (prev in _LEAD_WORDS
                 or (prev.isalpha() and len(prev) <= 4 and prev not in _COMMON_SHORT))
        )
        if len(run) >= 2 or lone_is_number:
            out.append(_number_value(run))
        else:
            out.extend(tokens[i:j])
        i = j
    return out


def _number_value(run: list[str]) -> str:
    if any(w in _TEENS or w in _TENS or w == "hundred" for w in run):
        value = 0
        for w in run:
            if w == "hundred":
                value = (value or 1) * 100
            else:
                value += _TENS.get(w) or _TEENS.get(w) or _UNITS[w]
        return str(value)
    # "two oh four" is read digit by digit, which is how room numbers are said.
    return "".join(str(_UNITS[w]) for w in run)


def _join_spelled_letters(tokens: list[str]) -> list[str]:
    """"c s 204" -> "cs 204", but only in front of a number, so "a b block" stays."""
    out: list[str] = []
    i = 0
    while i < len(tokens):
        j = i
        while j < len(tokens) and len(tokens[j]) == 1 and tokens[j].isalpha():
            j += 1
        if j - i >= 2 and j < len(tokens) and tokens[j].isdigit():
            out.append("".join(tokens[i:j]))
        else:
            out.extend(tokens[i:j] if j > i else [tokens[i]])
        i = max(j, i + 1)
    return out


def _contains(query: str, phrase: str) -> bool:
    return bool(phrase) and f" {phrase} " in f" {query} "


# -- retrieval -------------------------------------------------------------


@dataclass
class Hit:
    room: Room
    matched: str  # "code" | "name" | "alias"
    key: str
    score: float


@dataclass
class Retrieval:
    query: str
    normalized: str
    hits: list[Hit] = field(default_factory=list)
    ambiguous: bool = False
    blocks: list[str] = field(default_factory=list)
    listing: list[Room] = field(default_factory=list)
    listing_total: int = 0
    unknown_codes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "normalized": self.normalized,
            "hits": [
                {**asdict(h.room), "matched": h.matched, "key": h.key, "score": h.score}
                for h in self.hits
            ],
            "ambiguous": self.ambiguous,
            "blocks": self.blocks,
            "listing": [r.code for r in self.listing],
            "listing_total": self.listing_total,
            "unknown_codes": self.unknown_codes,
        }


_SCORES = {"code": 1.0, "name": 0.9, "alias": 0.9}


def retrieve(query: str, ds: Dataset) -> Retrieval:
    q = normalize(query)
    result = Retrieval(query=query, normalized=q)
    if not q:
        return result

    best: dict[str, Hit] = {}
    for room in ds.rooms:
        keys = [("code", room.code), ("name", room.name)] + [("alias", a) for a in room.aliases]
        for matched, value in keys:
            key = normalize(value)
            if not _contains(q, key):
                continue
            hit = Hit(room=room, matched=matched, key=key, score=_SCORES[matched])
            current = best.get(room.code)
            if current is None or (hit.score, len(hit.key)) > (current.score, len(current.key)):
                best[room.code] = hit

    hits = list(best.values())
    # A match inside a longer match belongs to the longer one: "computer science
    # lab 2" should not also pull in the room whose alias is "lab 2".
    hits = [
        h for h in hits
        if not any(o is not h and o.room.code != h.room.code and len(o.key) > len(h.key)
                   and _contains(o.key, h.key) for o in hits)
    ]
    hits.sort(key=lambda h: (-h.score, -len(h.key), h.room.code))
    result.hits = hits[:MAX_HITS]
    if len(hits) >= 2 and hits[0].key == hits[1].key:
        result.ambiguous = True
        result.hits = [h for h in hits if h.key == hits[0].key][:MAX_HITS]

    for block in ds.blocks():
        nb = normalize(block)
        if _contains(q, f"{nb} block") or _contains(q, f"block {nb}"):
            result.blocks.append(block)

    if not result.hits and result.blocks:
        wanted = {_TYPE_WORDS[t] for t in q.split() if t in _TYPE_WORDS}
        rooms = [
            r for r in ds.rooms
            if r.block in result.blocks and (not wanted or r.type in wanted)
        ]
        rooms.sort(key=lambda r: (r.block, r.floor, r.code))
        result.listing_total = len(rooms)
        result.listing = rooms[:MAX_LISTING]

    if not result.hits:
        known = {normalize(r.code) for r in ds.rooms}
        found: list[tuple[str, str]] = []
        # Written as a code ("ME-310", "it101"): a code, even when the letters
        # spell a word -- ME and IT are departments as well as words.
        for m in re.finditer(r"\b([A-Za-z]{1,4})-?(\d{2,4})\b", query):
            found.append((m.group(1).lower(), m.group(2)))
        # Spoken, with a space: only when the letters are not an ordinary word,
        # or "give me 10 minutes" would become a question about room ME-10.
        tokens = q.split()
        for a, b in zip(tokens, tokens[1:]):
            if a.isalpha() and len(a) <= 4 and a not in _COMMON_SHORT and b.isdigit() and len(b) >= 2:
                found.append((a, b))
        for a, b in found:
            code = f"{a.upper()}-{b}"
            if f"{a} {b}" not in known and code not in result.unknown_codes:
                result.unknown_codes.append(code)
    return result


# -- rendering -------------------------------------------------------------


def floor_words(floor: int) -> str:
    if floor < 0:
        return "basement" if floor == -1 else f"basement level {abs(floor)}"
    if floor < len(_ORDINALS):
        return f"{_ORDINALS[floor]} floor"
    return f"floor {floor}"


def _sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    return text if text[-1] in ".!?" else text + "."


def _directions(room: Room, ds: Dataset) -> str:
    """Hand-written directions win; otherwise the route through the graph."""
    if room.directions:
        return room.directions
    if room.node:
        steps = ds.route(room.node)
        if steps:
            return ", then ".join(steps)
    return ""


def _title(room: Room) -> str:
    if room.type == "facility" or not re.search(r"\d", room.code):
        return f"the {room.name}"
    return f"{room.code}, {room.name},"


def describe(room: Room, ds: Dataset) -> str:
    """One room as a person would say it at a desk."""
    where = f"in {room.block} block, on the {floor_words(room.floor)}"
    if room.wing:
        where += f", {room.wing} wing"
    if room.landmarks:
        where += f", {room.landmarks[0]}"
    text = _sentence(f"{_title(room)} is {where}")
    route = _directions(room, ds)
    if route:
        text += " " + _sentence(route if room.directions else f"from here, {route}")
    return text


def _entry(room: Room, ds: Dataset) -> str:
    parts = [f"{room.code}: {room.name}" + (f" ({room.type})" if room.type else "")]
    place = f"{room.block} block, {floor_words(room.floor)}"
    if room.wing:
        place += f", {room.wing} wing"
    parts.append(place)
    if room.department:
        parts.append(f"department {room.department}")
    if room.aliases:
        parts.append("also called " + ", ".join(room.aliases))
    if room.landmarks:
        parts.append("landmarks: " + "; ".join(room.landmarks))
    route = _directions(room, ds)
    if route:
        parts.append(f"directions from reception: {route}")
    if room.notes:
        parts.append(f"notes: {room.notes}")
    return "- " + ". ".join(parts)


def _coverage_line(ds: Dataset) -> str:
    labels = {"complete": "complete", "partial": "partly entered", "not_surveyed": "not surveyed yet"}
    bits = []
    for block in ds.blocks():
        cov = ds.coverage.get(block)
        if cov is None:
            bits.append(f"{block} block: some rooms entered")
            continue
        text = f"{block} block: {labels[cov.status]}"
        if cov.floors and cov.status in COVERAGE_STATUSES[:2]:
            text += " (" + ", ".join(floor_words(f) for f in cov.floors) + ")"
        bits.append(text)
    return "; ".join(bits)


def context(ds: Dataset, r: Retrieval) -> str:
    """The system-prompt section for this question."""
    if ds.empty:
        return (
            f"{CONTEXT_HEADER}: empty. You have not been given any campus "
            "information yet, so you do not know where any room or place is."
        )
    lines = [f"{CONTEXT_HEADER} (looked up for this question; the only campus facts you may use):"]
    if r.hits:
        if r.ambiguous:
            lines.append("Several entries match; ask which one they mean:")
        lines.extend(_entry(h.room, ds) for h in r.hits)
    elif r.listing:
        more = r.listing_total - len(r.listing)
        lines.append(f"Rooms in {' and '.join(b + ' block' for b in r.blocks)}"
                     + (f" ({r.listing_total} in all, first {len(r.listing)} shown):" if more else ":"))
        lines.extend(_entry(room, ds) for room in r.listing)
    else:
        lines.append("No directory entry matched this question.")
    if r.unknown_codes:
        lines.append("Not in the directory: " + ", ".join(r.unknown_codes) + ".")
    coverage = _coverage_line(ds)
    if coverage:
        lines.append(f"Survey status: {coverage}.")
    lines.append(f"The directory holds {len(ds.rooms)} entries in total.")
    return "\n".join(lines)


def offline_reply(ds: Dataset, r: Retrieval) -> str | None:
    """A spoken answer with no model at all, or None when there is none to give."""
    if r.hits and r.ambiguous:
        options = [f"{h.room.code} in {h.room.block} block" for h in r.hits[:3]]
        return "Did you mean " + ", or ".join(options) + "?"
    if r.hits:
        return " ".join(describe(h.room, ds) for h in r.hits[:2])
    if r.listing:
        names = [room.name for room in r.listing[:5]]
        more = r.listing_total - len(names)
        blocks = " and ".join(f"{b} block" for b in r.blocks)
        return (f"In {blocks} I know of " + ", ".join(names)
                + (f", and {more} more" if more > 0 else "") + ".")
    for block in r.blocks:
        cov = ds.coverage.get(block)
        if cov is None or cov.status == "not_surveyed":
            return f"I haven't learned {block} block yet."
    if r.unknown_codes and not ds.empty:
        return f"I don't have {r.unknown_codes[0]} in my directory yet."
    return None
