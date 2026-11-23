"""Working notes: one text per open assignment, the agent's scratchpad for long or detailed work.

They survive every context compression verbatim (a fresh session gets them in its first prompt, a
provider-side compaction gets a notice) and are dropped when the assignment ends (models: the status
listener); the report is what remains.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..events import bus
from ..models import Assignment, AssignmentStatus
from ..schemas import AssignmentOut
from .errors import ServiceError

CAP = 30_000
OPEN = (AssignmentStatus.QUEUED, AssignmentStatus.RUNNING)


async def current(session: AsyncSession, agent_id: int, assignment_id: int | None) -> Assignment | None:
    """The agent's open assignment: the one of this turn, else the one it is working on."""
    if assignment_id:
        a = await session.get(Assignment, assignment_id)
        return a if a is not None and a.agent_id == agent_id and a.status in OPEN else None
    q = select(Assignment).where(Assignment.agent_id == agent_id, Assignment.status == AssignmentStatus.RUNNING)
    return await session.scalar(q.order_by(Assignment.id.desc()).limit(1))


async def save(session: AsyncSession, assignment: Assignment, text: str) -> str:
    text = text.strip()
    if len(text) > CAP:
        raise ServiceError(f"Refused: your working notes would be {len(text)} chars, the cap is {CAP}. Condense them and write them whole with write_work_notes.")
    assignment.work_notes = text or None
    await session.commit()
    bus.publish(assignment.project_id, "assignment.upserted", AssignmentOut.model_validate(assignment))
    return f"Working notes saved ({len(text)} of {CAP} chars)."


async def append(session: AsyncSession, assignment: Assignment, text: str) -> str:
    before = assignment.work_notes or ""
    return await save(session, assignment, f"{before}\n{text.strip()}" if before else text)


def block(notes: str | None) -> str:
    """For the first prompt of a fresh session."""
    return f"Your working notes for this assignment (intact, verbatim; work_notes reads them again):\n<work_notes>\n{notes}\n</work_notes>" if notes else ""


def hint(notes: str | None) -> str:
    """For a session that goes on but may have lost them from view."""
    return f"Your working notes ({len(notes)} chars) are intact: read them with work_notes." if notes else ""
