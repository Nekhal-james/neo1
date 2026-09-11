"""Campus data: the Data tab's API.

Edits `intelligence.campus`'s three YAML files as text. The text is saved
verbatim, so an operator's comments survive, and only after it validates
together with the other two files as they are on disk -- a rooms.yaml that
names a graph node is checked against the graph.yaml that will actually be read
alongside it.

Nothing here restarts anything: `chat.ask` re-reads the files when they change,
so the next question sees a saved room.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import require_session

log = logging.getLogger(__name__)

router = APIRouter()

MAX_QUESTION_CHARS = 500


def _modules():
    try:
        from intelligence import campus, retrieval
        from intelligence.config import Config as IntelligenceConfig
    except ImportError as exc:
        raise HTTPException(501, f"intelligence is not installed: {exc}") from exc
    return campus, retrieval, IntelligenceConfig.load()


def _issues(validation) -> dict[str, Any]:
    return {
        "ok": validation.ok,
        "errors": [asdict(i) for i in validation.errors],
        "warnings": [asdict(i) for i in validation.warnings],
    }


def _file_name(campus, payload: dict) -> str:
    name = payload.get("file")
    if name not in campus.FILES:
        raise HTTPException(400, f"file must be one of {list(campus.FILES)}")
    return name


def _text(payload: dict) -> str:
    text = payload.get("text", "")
    if not isinstance(text, str):
        raise HTTPException(400, "text must be a string")
    return text


async def _json(request: Request) -> dict:
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "body must be JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "body must be a JSON object")
    return payload


@router.get("/api/campus")
async def get_campus(request: Request, user: str = Depends(require_session)) -> dict:
    """Everything the Data tab needs to draw itself: the files, what is wrong
    with them, the coverage summary, and the samples to copy from."""
    campus, _retrieval, cfg = _modules()
    data_dir = cfg.rag_data_path

    def read() -> dict:
        texts = campus.read_texts(data_dir)
        validation = campus.validate(texts)
        return {
            "data_dir": str(data_dir),
            "files": {
                name: {
                    "text": texts[name],
                    "exists": campus.file_path(data_dir, name).exists(),
                    "modified_ns": campus.modified_ns(data_dir, name),
                }
                for name in campus.FILES
            },
            "summary": campus.summary(validation.dataset),
            "samples": campus.SAMPLES,
            "room_types": list(campus.ROOM_TYPES),
            "coverage_statuses": list(campus.COVERAGE_STATUSES),
            **_issues(validation),
        }

    return await asyncio.to_thread(read)


@router.post("/api/campus/validate")
async def validate_campus(request: Request, user: str = Depends(require_session)) -> dict:
    """Check one file's draft against the other two as saved. Writes nothing."""
    campus, _retrieval, cfg = _modules()
    payload = await _json(request)
    name, text = _file_name(campus, payload), _text(payload)

    def check() -> dict:
        texts = campus.read_texts(cfg.rag_data_path)
        texts[name] = text
        validation = campus.validate(texts)
        return {**_issues(validation), "summary": campus.summary(validation.dataset)}

    return await asyncio.to_thread(check)


@router.post("/api/campus/save")
async def save_campus(request: Request, user: str = Depends(require_session)) -> dict:
    """Validate, then atomically replace one file, keeping a backup.

    `modified_ns` is the version the editor loaded. If the file changed since
    -- another tab, another operator, a hand edit over ssh -- the save is
    refused rather than silently overwriting that change.
    """
    campus, _retrieval, cfg = _modules()
    payload = await _json(request)
    name, text = _file_name(campus, payload), _text(payload)
    base = payload.get("modified_ns")
    data_dir, backup_dir = cfg.rag_data_path, cfg.rag_backup_path

    def save() -> dict:
        current = campus.modified_ns(data_dir, name)
        if base is not None and base != current:
            raise HTTPException(
                409,
                f"{name}.yaml changed on disk since you opened it, so it was not saved. "
                "Your edits are still in the editor: copy them, then Revert to load "
                "the new version.",
            )
        texts = campus.read_texts(data_dir)
        texts[name] = text
        validation = campus.validate(texts)
        if not validation.ok:
            raise HTTPException(422, {"message": "not saved: fix the errors first", **_issues(validation)})
        backup = campus.save_text(data_dir, name, text, backup_dir)
        log.info("campus %s.yaml saved by %s (backup: %s)", name, user, backup)
        return {
            **_issues(validation),
            "saved": name,
            "modified_ns": campus.modified_ns(data_dir, name),
            "backup": str(backup) if backup else None,
            "summary": campus.summary(validation.dataset),
        }

    return await asyncio.to_thread(save)


@router.post("/api/campus/try")
async def try_question(request: Request, user: str = Depends(require_session)) -> dict:
    """What a question retrieves, what the model would be shown, and what Neo
    says with no model -- without asking the model.

    `drafts` ({file: text}) tries unsaved edits, so an alias can be tested
    before it is committed to the SD card.
    """
    campus, retrieval, cfg = _modules()
    payload = await _json(request)
    question = str(payload.get("text", "")).strip()
    if not question:
        raise HTTPException(400, "text must not be empty")
    if len(question) > MAX_QUESTION_CHARS:
        raise HTTPException(413, f"text must be under {MAX_QUESTION_CHARS} characters")
    drafts = payload.get("drafts") or {}
    if not isinstance(drafts, dict) or any(k not in campus.FILES or not isinstance(v, str)
                                           for k, v in drafts.items()):
        raise HTTPException(400, f"drafts must map file names {list(campus.FILES)} to text")

    def run() -> dict:
        texts = campus.read_texts(cfg.rag_data_path)
        texts.update(drafts)
        ds = campus.validate(texts).dataset
        found = retrieval.retrieve(question, ds)
        return {
            **found.to_dict(),
            "context": retrieval.context(ds, found),
            "offline_reply": retrieval.offline_reply(ds, found),
            "used_drafts": sorted(drafts),
        }

    return await asyncio.to_thread(run)
