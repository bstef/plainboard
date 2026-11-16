"""What PlainBoard did by itself, as it happened.

The other tables keep the latest state: a branch is on a train, back or merged, and how it got
there is gone. This keeps the course: a train left with these branches, failed with that output,
was repaired by this agent; a wait was ended; an agent was paused. Nothing reads it at run time.
It exists for the work log (``services/trace.py``), with which the flow itself is improved.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import FlowEvent

REFS = ("agent_id", "run_id", "assignment_id", "task_id", "landing_id", "train_id")


async def record(session: AsyncSession, project_id: int, kind: str, **fields: Any) -> None:
    """Write one event. Ids of what it is about go into their columns, everything else into ``data``.
    Commits, like an inbox update: call it where the caller's own changes are already written."""
    refs = {key: fields.pop(key) for key in REFS if key in fields}
    session.add(FlowEvent(project_id=project_id, kind=kind, data=fields, **refs))
    await session.commit()
