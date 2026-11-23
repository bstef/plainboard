"""Scratchpad: one text per assignment, the agent's working memory for long or detail-heavy work.

It survives every context compression verbatim, and the agent is never relied on to look into it:
wherever it may have dropped out of view (a fresh session after compression, a provider-side compaction
mid-turn, a turn resumed after a restart or a lost connection) its content is delivered with it. Only
then and only when it is not empty: ordinary turns cost nothing. When the assignment ends it stays
archived, read-only, for ``scratchpad_retention_days``: a later assignment of the same ticket gets it at
its start (inline when short) and may take it over, the user sees it on the ticket. Then it goes (``purge``).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..events import bus
from ..models import Assignment, AssignmentStatus
from ..schemas import AssignmentOut
from . import settings as settings_service
from .errors import NotFound, ServiceError

CAP = 30_000
INLINE = 4000  # archived scratchpads up to this size (together) come inline at the start of a later assignment
EXCERPT = 500
OPEN = (AssignmentStatus.QUEUED, AssignmentStatus.RUNNING)


async def current(session: AsyncSession, agent_id: int, assignment_id: int | None) -> Assignment | None:
    """The agent's open assignment: the one of this turn, else the one it is working on."""
    if assignment_id:
        a = await session.get(Assignment, assignment_id)
        return a if a is not None and a.agent_id == agent_id and a.status in OPEN else None
    q = select(Assignment).where(Assignment.agent_id == agent_id, Assignment.status == AssignmentStatus.RUNNING)
    return await session.scalar(q.order_by(Assignment.id.desc()).limit(1))


async def archived(session: AsyncSession, assignment: Assignment, other_id: int) -> Assignment:
    """An ended assignment of the same ticket whose scratchpad is still kept."""
    other = await session.get(Assignment, other_id)
    if other is None or other.status in OPEN or not other.scratchpad or not assignment.task_id or other.task_id != assignment.task_id:
        raise NotFound(f"No archived scratchpad of assignment #{other_id} on this ticket.")
    return other


async def save(session: AsyncSession, assignment: Assignment, text: str) -> str:
    text = text.strip()
    if len(text) > CAP:
        raise ServiceError(f"Refused: the scratchpad would be {len(text)} chars, the cap is {CAP}. Condense it and write it whole (action write).")
    assignment.scratchpad = text or None
    await session.commit()
    bus.publish(assignment.project_id, "assignment.upserted", AssignmentOut.model_validate(assignment))
    return f"Scratchpad saved ({len(text)} of {CAP} chars)."


async def append(session: AsyncSession, assignment: Assignment, text: str) -> str:
    before = assignment.scratchpad or ""
    return await save(session, assignment, f"{before}\n{text.strip()}" if before else text)


async def restore(session: AsyncSession, assignment: Assignment, other_id: int) -> str:
    """Take an earlier assignment's scratchpad over as the start of this one's."""
    taken = (await archived(session, assignment, other_id)).scratchpad or ""
    return await save(session, assignment, f"{taken}\n{assignment.scratchpad}" if assignment.scratchpad else taken)


async def archived_block(session: AsyncSession, assignment: Assignment) -> str:
    """For the start of an assignment whose ticket has archived scratchpads (newest first): their
    content, inline while short, else an excerpt; nothing if there are none."""
    if not assignment.task_id or assignment.scratchpad:
        return ""
    q = select(Assignment).where(
        Assignment.task_id == assignment.task_id, Assignment.id != assignment.id, Assignment.status.not_in(OPEN), Assignment.scratchpad.is_not(None)
    )
    earlier = list(await session.scalars(q.order_by(Assignment.id.desc()).limit(3)))
    if not earlier:
        return ""
    parts, budget = [], INLINE
    for a in earlier:
        text = a.scratchpad or ""
        if len(text) <= budget:
            budget -= len(text)
            parts.append(f'<scratchpad assignment="#{a.id}">\n{text}\n</scratchpad>')
        else:
            parts.append(f'<scratchpad assignment="#{a.id}" excerpt="{EXCERPT} of {len(text)} chars">\n{text[:EXCERPT]}\n</scratchpad>')
    return (
        "Earlier assignments of this ticket left their scratchpads (archived; scratchpad with assignment_id reads one whole, restore takes it over as the start of yours):\n"
        + "\n".join(parts)
    )


async def purge(session: AsyncSession, now: datetime) -> int:
    """Drop archived scratchpads older than the retention period."""
    days = float(await settings_service.get(session, "scratchpad_retention_days"))
    q = update(Assignment).where(Assignment.scratchpad.is_not(None), Assignment.finished_at.is_not(None), Assignment.finished_at < now - timedelta(days=days))
    result = await session.execute(q.values(scratchpad=None))
    await session.commit()
    return result.rowcount or 0


def block(text: str | None) -> str:
    """For the first prompt of a fresh session."""
    return f"Your scratchpad for this assignment (intact, verbatim):\n<scratchpad>\n{text}\n</scratchpad>" if text else ""


def delivered(text: str | None) -> str:
    """For a session that goes on but may have lost it from view (compaction, resumed turn): the content itself."""
    return f"Your scratchpad for this assignment is intact, here it is verbatim:\n<scratchpad>\n{text}\n</scratchpad>" if text else ""
