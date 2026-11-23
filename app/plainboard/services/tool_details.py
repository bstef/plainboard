"""Full tool details are kept for ``tool_details_days`` (0: forever), then cut to a preview.

A tool message's ``meta`` holds the whole input and result of a step (a Bash output up to 20 000
characters): that is most of the database, and it mostly helps debugging. After the limit only what an
agent's context shows of a step stays (``context.render_message``): the name, the input and the result cut
to a preview, whether it failed, and ``trimmed``. Chat texts, tickets, reports and assignments are never
touched, nor the conversations of agents with an open assignment or a turn. Small batches, one
transaction each, so SQLite is never blocked for long. The space is reused by SQLite (no VACUUM here).
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Agent, Conversation, Message
from . import cleanup as cleanup_service
from . import settings as settings_service
from .util import truncate

INPUT_PREVIEW = 300
RESULT_PREVIEW = 400
BATCH = 200
FLOOR = "tool_details_floor"  # below this message id every tool message old enough was looked at


def days(settings: dict[str, Any]) -> float:
    try:
        return max(float(settings.get("tool_details_days") or 0), 0.0)
    except (TypeError, ValueError):
        return 0.0


def trimmed(meta: dict[str, Any], kept_days: float) -> dict[str, Any]:
    out: dict[str, Any] = {"name": meta.get("name"), "input": truncate(str(meta.get("input", "")), INPUT_PREVIEW)}
    if "tool_use_id" in meta:
        out["tool_use_id"] = meta["tool_use_id"]
    if "result" in meta:
        out["result"] = truncate(str(meta.get("result") or ""), RESULT_PREVIEW)
    out["is_error"] = bool(meta.get("is_error"))
    out["trimmed"] = True
    out["trimmed_days"] = int(kept_days) if kept_days == int(kept_days) else kept_days
    return out


def _size(meta: dict[str, Any]) -> int:
    return len(json.dumps(meta, ensure_ascii=False).encode())


async def trim(session: AsyncSession, now: datetime, batch: int = BATCH) -> dict[int, tuple[int, int]]:
    """Trim the tool messages older than the limit; per project the messages trimmed and the bytes freed."""
    settings = await settings_service.get_all(session)
    kept = days(settings)
    if kept <= 0:
        return {}
    cutoff = now - timedelta(days=kept)
    busy = await cleanup_service.busy_agents(session, jobs=False)
    projects = dict((await session.execute(select(Conversation.id, Agent.project_id).join(Agent, Agent.id == Conversation.agent_id))).all())
    skip = set(await session.scalars(select(Conversation.id).where(Conversation.agent_id.in_(busy)))) if busy else set()
    floor = int(settings.get(FLOOR) or 0)
    last, first_skipped = floor - 1, None
    done: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    while True:
        rows = (
            await session.scalars(
                select(Message).where(Message.kind == "tool", Message.id > last, Message.created_at < cutoff).order_by(Message.id).limit(batch)
            )
        ).all()
        if not rows:
            break
        for msg in rows:
            last = msg.id
            meta = msg.meta or {}
            if meta.get("trimmed"):
                continue
            if msg.conversation_id in skip:
                first_skipped = first_skipped if first_skipped is not None else msg.id
                continue
            short = trimmed(meta, kept)
            msg.meta = short
            counted = done[projects.get(msg.conversation_id, 0)]
            counted[0] += 1
            counted[1] += max(0, _size(meta) - _size(short))
        await session.commit()
        session.expunge_all()  # the batch's big metas are not kept in memory
        await asyncio.sleep(0)
    new_floor = first_skipped if first_skipped is not None else last + 1
    if new_floor != floor:
        await settings_service.update(session, {FLOOR: new_floor}, managed=True)
    return {pid: (n, size) for pid, (n, size) in done.items() if n}


def notice(count: int, size: int, kept: float) -> str:
    return f"Trimmed tool details of {count} messages older than {kept:g} days ({size / 1_000_000:.1f} MB)."
