"""A person's short question about a project, answered at once beside the chat (orchestration/questions.py)."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..services import projects as project_service
from .deps import Runs, Session, Who

router = APIRouter(prefix="/api", tags=["questions"])


class QuestionIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    thread: str | None = Field(default=None, max_length=100)  # a follow-up: the conversation it goes on


@router.post("/projects/{project_id}/questions")
async def ask(project_id: int, body: QuestionIn, session: Session, runs: Runs, who: Who) -> StreamingResponse:
    """The answer as lines of JSON while it comes (``Questions.stream``). Only the person asking gets it."""
    project = await project_service.get(session, project_id)
    thread, spec, ctx = await runs.questions.start(session, project, who.user, body.text, body.thread)
    await session.close()  # the answer can take minutes: no database connection is held for it
    headers = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
    return StreamingResponse(runs.questions.stream(thread, spec, ctx), media_type="application/x-ndjson", headers=headers)


@router.delete("/projects/{project_id}/questions/{thread_id}", status_code=204)
async def close(project_id: int, thread_id: str, runs: Runs, who: Who) -> None:
    """The window was closed: its conversation is over."""
    runs.questions.close(thread_id, project_id, who.user)
