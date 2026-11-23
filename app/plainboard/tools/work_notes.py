"""Working notes of the agent's current assignment (services/work_notes.py)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..models import Assignment
from ..services import work_notes
from .registry import WORKER, ToolContext, ToolError, tool


class NotesArgs(BaseModel):
    text: str = Field(description="Markdown")


async def _assignment(ctx: ToolContext) -> Assignment:
    assignment = await work_notes.current(ctx.session, ctx.agent.id, ctx.run.assignment_id)
    if assignment is None:
        raise ToolError("Working notes belong to an open assignment, and you have none.")
    return assignment


@tool("work_notes", "Read your working notes of the current assignment.", roles=(WORKER,))
async def read_notes(ctx: ToolContext, _args) -> str:
    return (await _assignment(ctx)).work_notes or "(no working notes yet)"


@tool("write_work_notes", f"Replace your working notes of the current assignment (max {work_notes.CAP} chars).", NotesArgs, roles=(WORKER,))
async def write_notes(ctx: ToolContext, args: NotesArgs) -> str:
    return await work_notes.save(ctx.session, await _assignment(ctx), args.text)


@tool("append_work_notes", "Add a line or block to your working notes.", NotesArgs, roles=(WORKER,))
async def append_notes(ctx: ToolContext, args: NotesArgs) -> str:
    return await work_notes.append(ctx.session, await _assignment(ctx), args.text)
