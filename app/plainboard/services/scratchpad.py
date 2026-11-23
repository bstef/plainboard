"""Scratchpad: one text per assignment, the agent's working memory for long or detail-heavy work.

It survives every context compression verbatim: a fresh session gets it in its first prompt, a
provider-side compaction and a resumed turn get a one-line notice (never on ordinary turns). When the
assignment ends it stays archived, read-only, for ``scratchpad_retention_days``: a later assignment of
the same ticket may read or take it over, the user sees it on the ticket. Then it goes (``purge``).
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


async def archived_line(session: AsyncSession, assignment: Assignment) -> str:
    """One line for the start of an assignment whose ticket has archived scratchpads, else nothing."""
    if not assignment.task_id or assignment.scratchpad:
        return ""
    q = select(Assignment.id).where(
        Assignment.task_id == assignment.task_id, Assignment.id != assignment.id, Assignment.status.not_in(OPEN), Assignment.scratchpad.is_not(None)
    )
    ids = list(await session.scalars(q.order_by(Assignment.id.desc()).limit(5)))
    if not ids:
        return ""
    return f"Earlier assignments of this ticket left a scratchpad ({', '.join(f'#{i}' for i in ids)}): scratchpad read or restore with assignment_id, if useful."


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


def hint(text: str | None) -> str:
    """For a session that goes on but may have lost it from view."""
    return f"Your scratchpad ({len(text)} chars) is intact: read it with scratchpad." if text else ""
