"""Where a ticket stands: one text per ticket, kept by PlainBoard from the reports of its assignments.

A ticket that takes many assignments has no memory of its own. Every agent starts with a fresh
context and the manager's is compressed again and again, so what was done, decided and tried lives in
dozens of reports that nobody reads in full. Work then goes in circles: something is done twice, an
approach that failed is tried again, a measurement is repeated.

The state is that memory. It is not written by the agents (that would cost each of them a step and
depend on their discipline) and it is no list that grows like comments: when a few new reports have
come in, the small model rewrites the one text from the state so far and those reports. Agents get
it with the ticket (``get_task``) and in front of every assignment on it; the user sees it in the
ticket.

A ticket that is moved to done gets its state written once more (``closed``), with the reports that
were not in it yet and with what PlainBoard itself knows: which of its branches landed. Seen in
production: a ticket on done still said "Open: not yet on main", from the last rewrite before it
landed. Someone who reopens it must not do that again.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Assignment, AssignmentStatus, Landing, LandingStatus, Project, Task, TaskStatus, TaskTransition, TicketState, utcnow
from .util import truncate

FROM_REPORTS = 3  # a ticket with fewer reports needs no state: the reports themselves say it
EVERY = 3  # rewritten when this many new reports have come in
REPORT_CHARS = 2500  # of each report that goes into it
STATE_CHARS = 2500  # asked for
STORED_CHARS = 6000  # kept: a state that runs over is not cut in the middle of what matters

SYSTEM = f"""You keep the state of one ticket for a team of AI agents and its user. You get the ticket, its state so far and the reports of work that was finished since. Write the new state: what someone who starts on this ticket now must know, so that nothing is done twice and nothing that failed is tried again.

Rules:
- Only facts from the state so far and the reports. No guesses, no advice, no praise.
- Keep exact names: branches, pull requests, files, commits, measured numbers, assignment numbers (#123).
- Replace what is outdated: a measurement that was repeated, an open point that is done now. The newer report wins.
- Plain text in these four parts, in this order, each a short list. Leave a part out when it is empty.
Open: what is still to do or unclear, the most important first.
Tried and dropped: approaches that did not work, each with the reason in a few words.
Decided: decisions of the user or the team that still hold.
Done: what is finished, and where it is (on the base branch, on a branch that has not landed, only measured). The shortest part: one line per result, old steps merged.
- A closed ticket (the input says so): call the first part "Left open:" instead of "Open:". It lists only what the reports say was not done or not checked and nothing later covered: a known gap, not work in progress. What only waited for a branch to land is done once the input says that branch landed. Leave the part out when nothing is left.
- At most {STATE_CHARS} characters in all: shorten Done first. English."""

REPORTED = (AssignmentStatus.DONE, AssignmentStatus.FAILED)


async def get(session: AsyncSession, task_id: int) -> TicketState | None:
    return await session.get(TicketState, task_id)


async def text_for(session: AsyncSession, task_id: int | None) -> str:
    state = await session.get(TicketState, task_id) if task_id else None
    return state.text if state else ""


def _done_at():
    """When a ticket was last moved to done (a subquery for a query over tickets)."""
    moved = (TaskTransition.project_id == Task.project_id, TaskTransition.task_number == Task.number, TaskTransition.status == TaskStatus.DONE)
    return select(func.max(TaskTransition.at)).where(*moved).correlate(Task).scalar_subquery()


async def closed(session: AsyncSession, task: Task) -> str:
    """For a ticket on done whose state is from before that: what the rewrite is told. Else ""."""
    state = await get(session, task.id)
    if state is None or task.status != TaskStatus.DONE:
        return ""
    done_at: datetime | None = await session.scalar(select(_done_at()).select_from(Task).where(Task.id == task.id))
    if done_at is None or state.updated_at >= done_at:
        return ""
    landed = await session.scalars(select(Landing).where(Landing.task_id == task.id, Landing.status == LandingStatus.MERGED).order_by(Landing.id))
    branches = ", ".join(f"{landing.branch} ({landing.merged_at:%Y-%m-%d %H:%M} UTC)" if landing.merged_at else landing.branch for landing in landed)
    return f"The ticket is closed: it was moved to done on {done_at:%Y-%m-%d %H:%M} UTC. Branches of this ticket that landed on the base branch: {branches or 'none recorded'}."


async def behind_closed(session: AsyncSession, limit: int) -> list[int]:
    """Tickets on done whose state is from before they were moved there: the newest first."""
    q = (
        select(Task.id)
        .join(TicketState, TicketState.task_id == Task.id)
        .join(Project, Project.id == Task.project_id)
        .where(Task.status == TaskStatus.DONE, Task.archived_at.is_(None), Project.paused_at.is_(None), TicketState.text != "", TicketState.updated_at < _done_at())
        .order_by(Task.id.desc())
        .limit(limit)
    )
    return list(await session.scalars(q))


async def due(session: AsyncSession, task: Task, final: bool = False) -> list[Assignment]:
    """The reports that are not in the state yet, if it is time to rewrite it; else none.
    ``final``: the ticket is closed, so all of them, however few."""
    state = await get(session, task.id)
    reported = (Assignment.task_id == task.id, Assignment.status.in_(REPORTED), Assignment.result.is_not(None))
    total = await session.scalar(select(func.count()).select_from(Assignment).where(*reported)) or 0
    if total < FROM_REPORTS:
        return []
    new = list(await session.scalars(select(Assignment).where(*reported, Assignment.id > (state.until_assignment_id if state else 0)).order_by(Assignment.id)))
    return new if final or len(new) >= (EVERY if state else 1) else []


def prompt(task: Task, state: str, reports: list[Assignment], names: dict[int, str], closed: str = "") -> str:
    listed = "\n\n".join(f"Assignment #{a.id} ({names.get(a.agent_id, '?')}, {a.status}): {a.title}\n{truncate(a.result or '', REPORT_CHARS)}" for a in reports)
    return (
        f"Ticket #{task.number}: {task.title}\n{truncate(task.description.strip(), 1500)}\n\n"
        f"State so far:\n{state.strip() or '(none yet)'}\n\n"
        f"Reports since, oldest first:\n\n{listed or '(none)'}" + (f"\n\n{closed}" if closed else "")
    )


async def store(session: AsyncSession, task: Task, text: str, until_assignment_id: int) -> TicketState:
    state = await get(session, task.id) or TicketState(task_id=task.id)
    state.text, state.until_assignment_id, state.updated_at = truncate(text.strip(), STORED_CHARS), until_assignment_id, utcnow()
    session.add(state)
    await session.commit()
    return state


def lines(state: TicketState | None) -> list[str]:
    """The state as an agent sees it with the ticket."""
    if state is None or not state.text:
        return []
    return [f"Where it stands (kept by PlainBoard from the reports up to assignment #{state.until_assignment_id}; newer ones are below):", state.text]
