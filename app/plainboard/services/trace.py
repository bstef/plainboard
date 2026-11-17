"""The work log: everything of a period as it happened, to find out where a flow wastes turns.

Unlike the work report (numbers only) this holds the texts: every turn with what it was given and
what it answered, every tool call with input and result, assignments, updates, jobs, landing and
PlainBoard's own decisions (``flow_log``). One JSON object per line, gzip, streamed: a month of a
busy project is hundreds of thousands of rows. No credentials: encrypted columns and hashes are
left out, a user and password inside a URL are blanked wherever they appear (agents see repository
URLs in their prompt), and secret values never were in the stored messages.
"""

from __future__ import annotations

import json
import re
import zlib
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import sessionmaker
from ..models import (
    Agent,
    AgentNote,
    Assignment,
    Comment,
    Conversation,
    Decision,
    FlowEvent,
    InboxItem,
    Job,
    Landing,
    Message,
    Note,
    Project,
    Repository,
    Run,
    Schedule,
    Task,
    TaskTransition,
    Todo,
    Train,
    utcnow,
)
from . import projects as project_service

VERSION = 1
BATCH = 500
# Never exported, whatever the table: credentials and what only repeats other rows.
LEFT_OUT = {"token_user", "ssh_public_key", "provider_session_id", "prompt_sections", "session_sections", "storage_name"}
URL_LOGIN = re.compile(r"(://)[^/\s:@\"]+:[^/\s@\"]+@")  # https://user:token@host


def exported(key: str) -> bool:
    return not (key.endswith(("_enc", "_hash")) or key in LEFT_OUT)


def as_row(kind: str, obj: Any, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"type": kind}
    for attr in inspect(type(obj)).column_attrs:
        if exported(attr.key):
            value = getattr(obj, attr.key)
            out[attr.key] = value.isoformat() + "Z" if isinstance(value, datetime) else value
    return out | extra


async def rows(session: AsyncSession, project_id: int, days: int) -> AsyncIterator[dict[str, Any]]:
    since = utcnow() - timedelta(days=days)
    project = await session.get(Project, project_id)
    yield {
        "type": "export",
        "version": VERSION,
        "generated_at": utcnow().isoformat() + "Z",
        "days": days,
        "since": since.isoformat() + "Z",
        "max_parallel_runs": await project_service.parallel_limit(session, project),
        "note": "One object per line; `type` names the table. Times are UTC. Rows that describe the project (agents, notes, tickets) are complete, the rest is of the period. "
        "A message of kind tool keeps the call's input up to 1500 characters and its result up to 2000; what was ordered, sent and answered is complete in assignment, update and run.",
    }
    yield as_row("project", project)

    agents = list(await session.scalars(select(Agent).where(Agent.project_id == project_id).order_by(Agent.id)))
    agent_ids = [a.id for a in agents]
    conversations = list(await session.scalars(select(Conversation).where(Conversation.agent_id.in_(agent_ids)).order_by(Conversation.id)))
    agent_of = {c.id: c.agent_id for c in conversations}
    task_ids = select(Task.id).where(Task.project_id == project_id)

    # What the project is: small, and needed to read the rest.
    whole: list[tuple[str, Any]] = [
        ("repository", select(Repository).where(Repository.project_id == project_id).order_by(Repository.id)),
        ("note", select(Note).where(Note.project_id == project_id).order_by(Note.id)),
        ("agent_note", select(AgentNote).where(AgentNote.agent_id.in_(agent_ids)).order_by(AgentNote.id)),
        ("todo", select(Todo).where(Todo.agent_id.in_(agent_ids)).order_by(Todo.id)),
        ("schedule", select(Schedule).where(Schedule.project_id == project_id).order_by(Schedule.id)),
        ("ticket", select(Task).where(Task.project_id == project_id).order_by(Task.id)),
    ]
    for agent in agents:
        yield as_row("agent", agent)
    for conversation in conversations:
        yield as_row("conversation", conversation)

    # What happened in the period, table by table, each in the order it happened.
    period: list[tuple[str, Any]] = [
        ("ticket_move", select(TaskTransition).where(TaskTransition.project_id == project_id, TaskTransition.at >= since).order_by(TaskTransition.id)),
        ("comment", select(Comment).where(Comment.task_id.in_(task_ids), Comment.created_at >= since).order_by(Comment.id)),
        ("decision", select(Decision).where(Decision.project_id == project_id, Decision.created_at >= since).order_by(Decision.id)),
        ("assignment", select(Assignment).where(Assignment.project_id == project_id, Assignment.created_at >= since).order_by(Assignment.id)),
        ("run", select(Run).where(Run.project_id == project_id, Run.created_at >= since).order_by(Run.id)),
        ("update", select(InboxItem).where(InboxItem.agent_id.in_(agent_ids), InboxItem.created_at >= since).order_by(InboxItem.id)),
        ("job", select(Job).where(Job.project_id == project_id, Job.created_at >= since).order_by(Job.id)),
        ("landing", select(Landing).where(Landing.project_id == project_id, Landing.updated_at >= since).order_by(Landing.id)),
        ("train", select(Train).where(Train.project_id == project_id, Train.created_at >= since).order_by(Train.id)),
        ("flow_event", select(FlowEvent).where(FlowEvent.project_id == project_id, FlowEvent.at >= since).order_by(FlowEvent.id)),
        ("message", select(Message).where(Message.conversation_id.in_(list(agent_of)), Message.created_at >= since).order_by(Message.id)),
    ]
    for kind, query in [*whole, *period]:
        result = await session.stream_scalars(query.execution_options(yield_per=BATCH))
        async for obj in result:
            yield as_row(kind, obj, agent_id=agent_of.get(obj.conversation_id)) if kind == "message" else as_row(kind, obj)


async def stream(project_id: int, days: int) -> AsyncIterator[bytes]:
    """The work log as gzip'ed JSON lines. Has its own session: it outlives the request that asked for it."""
    packer = zlib.compressobj(6, zlib.DEFLATED, 31)
    async with sessionmaker()() as session:
        async for row in rows(session, project_id, days):
            line = URL_LOGIN.sub(r"\1[login]@", json.dumps(row, ensure_ascii=False, default=str))
            if chunk := packer.compress(line.encode() + b"\n"):
                yield chunk
    yield packer.flush()
