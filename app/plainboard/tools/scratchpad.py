"""The scratchpad of the agent's current assignment (services/scratchpad.py)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..services import scratchpad
from .registry import WORKER, ToolContext, ToolError, tool


class ScratchpadArgs(BaseModel):
    action: Literal["read", "write", "append", "restore"] = "read"
    text: str = Field(default="", description="write: whole text; append: what to add")
    assignment_id: int | None = Field(default=None, description="read/restore: an earlier assignment of this ticket")


@tool(
    "scratchpad",
    f"Your assignment's scratchpad (not project/own notes), max {scratchpad.CAP} chars; survives context compression. write replaces, restore takes over an earlier one.",
    ScratchpadArgs,
    roles=(WORKER,),
)
async def scratchpad_tool(ctx: ToolContext, args: ScratchpadArgs) -> str:
    assignment = await scratchpad.current(ctx.session, ctx.agent.id, ctx.run.assignment_id)
    if assignment is None:
        raise ToolError("A scratchpad belongs to an open assignment, and you have none.")
    if args.action == "read":
        target = await scratchpad.archived(ctx.session, assignment, args.assignment_id) if args.assignment_id else assignment
        return target.scratchpad or "(empty)"
    if args.action == "restore":
        if not args.assignment_id:
            raise ToolError("restore needs assignment_id.")
        return await scratchpad.restore(ctx.session, assignment, args.assignment_id)
    if args.action == "write":
        return await scratchpad.save(ctx.session, assignment, args.text)
    return await scratchpad.append(ctx.session, assignment, args.text)
