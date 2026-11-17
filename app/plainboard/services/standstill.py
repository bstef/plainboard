"""Ready work next to free slots: the manager is told, once per situation.

Every manager turn already shows the free slots and who is idle. What no prompt can do is start a
turn: when the manager's last turn ended without ordering what could start, and nothing runs that
would report back, no event ever comes, and the project stands until the user writes. So the board is
looked at instead, without a model (``orchestration/standstill.py``): tickets to do or in progress
that nothing happens on and that don't wait for the user, a free slot and a member with nothing to
do, for a while after the manager's last turn. Then the manager gets one turn with exactly that, and
decides: start it, or leave it.

It is not brought up again while nothing changes, and after a change not before an hour has passed.
A project with nothing to do, or one that works one step at a time on purpose, costs nothing or
next to nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ACTIVE_RUN_STATUSES, Agent, Assignment, Project, Run, Task, TaskStatus
from . import board as board_service
from .timing import duration
from .util import truncate

QUIET_SECONDS = 15 * 60  # after the manager's last turn: it has just looked at the same board
RETELL_SECONDS = 60 * 60  # after it was told: not again before this, whatever has changed
MAX_TICKETS = 8


@dataclass
class Standstill:
    manager_id: int
    tickets: list[Task]
    free: int
    limit: int
    idle: list[str]
    quiet_seconds: float
    in_flight: frozenset[int]  # the open assignments: one that ends may free what waited for it

    @property
    def situation(self) -> tuple[frozenset[int], frozenset[int]]:
        """What the manager is told about. The same one again is no news."""
        return frozenset(t.id for t in self.tickets), self.in_flight


async def find(session: AsyncSession, project: Project, limit: int, waiting: frozenset[int] | set[int], now: datetime) -> Standstill | None:
    """``limit``: the project's parallel members. ``waiting``: turns that only wait for a job (they take no slot)."""
    if not project.auto_wake_manager or limit < 1:
        return None
    manager = await session.scalar(select(Agent).where(Agent.project_id == project.id, Agent.is_manager, Agent.enabled))
    if manager is None:
        return None
    if await session.scalar(select(Run.id).where(Run.agent_id == manager.id, Run.status.in_(ACTIVE_RUN_STATUSES)).limit(1)):
        return None  # it is at it, or about to be
    last = await session.scalar(select(func.max(Run.finished_at)).where(Run.agent_id == manager.id)) or project.created_at
    quiet = (now - last).total_seconds()
    if quiet < QUIET_SECONDS:
        return None
    capacity = await board_service.capacity(session, project.id, limit, except_run_id=0, waiting=waiting)
    if not capacity.free or not capacity.idle:
        return None
    tickets = await board_service.untouched(session, project.id, (TaskStatus.IN_PROGRESS, TaskStatus.TODO))
    if not tickets:
        return None
    in_flight = frozenset(await session.scalars(select(Assignment.id).where(Assignment.project_id == project.id, Assignment.status.in_(board_service.OPEN_ASSIGNMENT_STATUSES))))
    return Standstill(manager.id, tickets, capacity.free, limit, capacity.idle, quiet, in_flight)


def text(found: Standstill) -> str:
    """The update the manager gets. It has no clock: how long nothing happened is said."""
    lines = [f"- #{t.number} {truncate(t.title, 80)} ({t.status.replace('_', ' ')})" for t in found.tickets[:MAX_TICKETS]]
    more = f"\n(+{len(found.tickets) - MAX_TICKETS} more: list_tasks)" if len(found.tickets) > MAX_TICKETS else ""
    return (
        f"PlainBoard: for {duration(found.quiet_seconds)} nothing was ordered, {found.free} of {found.limit} member slots are free "
        f"(idle: {', '.join(found.idle[:8])}), and nothing happens on these tickets, which don't wait for the user:\n"
        + "\n".join(lines)
        + more
        + "\nStart what can start now, on several members where the work is independent. Leave a ticket only if it depends on running "
        "work, would collide with it, or the user wants it later: then say which in one line. You are not asked again while nothing changes."
    )
