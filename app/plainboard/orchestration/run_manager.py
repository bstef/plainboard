"""Run orchestration: queue, dispatch, execution, inter-agent communication and run lifecycle.

Rules:
- One run per agent at a time; runs of different members execute in parallel up to
  ``max_parallel_runs``. The limit is about members only: a manager's turn starts as soon as the
  manager is free, also at limit 0, because the user and the team wait for it.
  Nested runs (an agent waiting on ``ask_agent``) bypass the limit so a waiting agent can never
  starve the run it waits for.
- A wait inside a turn (``wait_job``, ``ask_agent``) never holds back an answer: it ends when the
  user writes to the agent, and a wait for a job also when a teammate asks it something.
- ``ask_agent`` blocks the caller; a wait-for graph between agents rejects cycles (deadlocks).
- An assignment is one piece of work with one report. It stays open while a job started for it is
  running: the job's result continues it in a later turn, and only the turn after which nothing of
  it is running any more closes it and sends the report.
- A member's context belongs to its piece of work: an assignment for another ticket starts a new session.
- Messages between agents and results for the requester go to the inbox; an agent is woken up once
  per batch of updates (coalesced).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from plainboard_protocol import JobSpec, JobState, ResultEvent, RunSpec, ToolBridgeSpec, ToolCallResult, WorkspaceSpec
from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import get_config
from ..events import bus
from ..models import (
    ACTIVE_RUN_STATUSES,
    Agent,
    Assignment,
    AssignmentStatus,
    Conversation,
    Decision,
    Job,
    JobStatus,
    Message,
    Project,
    Run,
    RunStatus,
    RunTrigger,
    Task,
    TaskStatus,
    utcnow,
)
from ..schemas import AssignmentOut, MessageOut, RunOut, TaskUpdate
from ..services import agents as agent_service
from ..services import board as board_service
from ..services import decisions as decision_service
from ..services import flow_log
from ..services import jobs as job_service
from ..services import mcp as mcp_service
from ..services import projects as project_service
from ..services import repositories as repo_service
from ..services import secrets as secret_service
from ..services import timing as timing_service
from ..services import todos as todo_service
from ..services import accounts as account_service
from ..services import limits as limits_service
from ..services import settings as settings_service
from ..services.errors import NotFound, ServiceError, WaitEnded
from ..services.util import truncate
from ..tools import registry
from . import inbox, prompts
from .context import ContextManager, workspace_path
from .runner_client import RunnerClient, RunnerError

log = logging.getLogger(__name__)

MAX_ASK_DEPTH = 3
ASK_TIMEOUT_SECONDS = 900
ASK_BUSY_TIMEOUT_SECONDS = 120  # the teammate is in a turn of unknown length, so don't block the asker for long
CANCEL_GRACE_SECONDS = 15
RESTARTED = "Interrupted by a restart"
MAX_RESTART_RESUMES = 3  # an assignment cut off this often by restarts goes to the manager instead of on


@dataclass
class RunContext:
    run_id: int
    project_id: int
    agent_id: int
    token: str
    redact: Callable[[str], str] = lambda text: text  # hides secret values in what is stored and shown
    account_key: str | None = None  # the provider account the run uses (its limit reports belong to it)
    assignment_id: int | None = None  # the piece of work this turn belongs to
    task_id: int | None = None


def _count_and_age(stats: tuple[int, datetime | None] | None) -> tuple[int, int]:
    """Open tasks and the age of the oldest in days."""
    if not stats or not stats[1]:
        return (stats[0] if stats else 0), 0
    return stats[0], (utcnow() - stats[1]).days


@dataclass
class Outcome:
    result: ResultEvent | None = None
    error: str | None = None
    cancelled: bool = False
    last_text: str | None = None
    tool_messages: dict[str, int] = field(default_factory=dict)
    resumed: bool = False


class RunManager:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], runner: RunnerClient):
        self._sm = sessionmaker
        self.runner = runner
        self.context = ContextManager(runner)
        self._running: dict[int, asyncio.Task[None]] = {}
        self._unlimited: set[int] = set()  # running turns outside the parallel limit: nested ones and managers'
        self._busy_agents: set[int] = set()
        self._job_news: set[int] = set()  # agents whose open assignment has a job result they have not read yet
        self._job_waits: dict[int, float] = {}  # run id -> seconds its turn spent waiting for jobs
        self._tokens: dict[str, RunContext] = {}
        self._waiters: dict[int, asyncio.Future[str]] = {}
        self._orphan_asks: set[int] = set()
        self._job_waiters: dict[int, asyncio.Future[str]] = {}  # job id -> the turn waiting for its result
        # run id -> what its turn waits for right now ("job" or "ask"), each with the way to end that wait
        self._turn_waits: dict[int, list[tuple[str, asyncio.Future[str]]]] = {}
        self._waits_for: dict[int, dict[int, int]] = {}
        self._cancel_requested: set[int] = set()
        self._limits_asked = 0.0
        self._limits_task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._dispatcher: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        await self._recover_interrupted()
        async with self._sm() as s:
            # An assignment that waited for a job when the app stopped must not wait forever.
            for agent_id in set(await s.scalars(select(Assignment.agent_id).where(Assignment.status == AssignmentStatus.RUNNING))):
                try:
                    await self._settle(s, agent_id)
                except Exception:
                    log.exception("Could not continue the open assignment of agent %s", agent_id)
        self._dispatcher = asyncio.create_task(self._dispatch_loop(), name="run-dispatcher")
        self._wake.set()

    async def _recover_interrupted(self) -> None:
        async with self._sm() as s:
            now = utcnow()
            interrupted = list((await s.scalars(select(Run).where(Run.status == RunStatus.RUNNING))).all())
            resumed: set[int] = set()
            for run in interrupted:
                run.status, run.error, run.finished_at = RunStatus.FAILED, RESTARTED, now
                if run.assignment_id and (a := await s.get(Assignment, run.assignment_id)):
                    # A restart is not a failure of the work: the assignment stays open and its agent
                    # goes on in the same session. Only one that keeps being cut off goes to the manager.
                    cut_off = await s.scalar(select(func.count()).select_from(Run).where(Run.assignment_id == a.id, Run.error == RESTARTED, Run.id != run.id)) or 0
                    if a.status == AssignmentStatus.RUNNING and cut_off + 1 < MAX_RESTART_RESUMES:
                        resumed.add(run.id)
                    else:
                        a.status, a.result, a.finished_at = AssignmentStatus.FAILED, RESTARTED, now
            for run in (await s.scalars(select(Run).where(Run.status == RunStatus.QUEUED, Run.trigger == RunTrigger.ASK))).all():
                run.status, run.error, run.finished_at = RunStatus.CANCELLED, "Asking agent is gone (restart)", now
            await s.commit()
            for run in interrupted:
                try:
                    await flow_log.record(
                        s, run.project_id, "run.interrupted", agent_id=run.agent_id, run_id=run.id, assignment_id=run.assignment_id, task_id=run.task_id,
                        trigger=str(run.trigger), resumed=run.id in resumed,
                    )  # fmt: skip
                    if run.id in resumed:
                        await inbox.push(s, run.agent_id, prompts.RESTART_NOTE)
                        await self._settle(s, run.agent_id)
                    else:
                        await self._report_interrupted(s, run)
                except Exception:  # a failed report must not keep the app from starting
                    log.exception("Could not report interrupted run %s", run.id)

    async def _report_interrupted(self, s: AsyncSession, run: Run) -> None:
        """An interrupted turn outside an open assignment is not retried (it may be half done): tell whoever can decide."""
        agent = await s.get(Agent, run.agent_id)
        project = await s.get(Project, run.project_id)
        if agent is None or project is None:
            return
        assignment = await s.get(Assignment, run.assignment_id) if run.assignment_id else None
        if assignment:
            ticket = await s.get(Task, assignment.task_id) if assignment.task_id else None
            ref = f", ticket #{ticket.number}" if ticket else ""
            recipient = assignment.created_by_agent_id or (await agent_service.get_manager(s, project.id)).id
            await inbox.push(
                s, recipient, f'Assignment #{assignment.id} "{assignment.title}" ({agent.name}{ref}) was interrupted by a restart and may be half done. Check its state and delegate again if needed.'
            )
            await self._maybe_wake(s, project, recipient)
        elif agent.is_manager:
            await inbox.push(s, agent.id, f"Your previous turn was interrupted by a restart and may be incomplete: {truncate(run.input_text or 'processing updates', 500)}")
            await self._maybe_wake(s, project, agent.id)

    async def stop(self) -> None:
        if self._dispatcher:
            self._dispatcher.cancel()
        for task in list(self._running.values()):
            task.cancel()
        await asyncio.gather(*self._running.values(), return_exceptions=True)

    def context_for_token(self, token: str, run_id: int) -> RunContext | None:
        ctx = self._tokens.get(token)
        return ctx if ctx and ctx.run_id == run_id else None

    # ------------------------------------------------------------------ enqueueing

    async def enqueue(
        self,
        s: AsyncSession,
        agent: Agent,
        trigger: RunTrigger,
        text: str,
        *,
        task_id: int | None = None,
        assignment_id: int | None = None,
        schedule_id: int | None = None,
        source_agent_id: int | None = None,
        parent_run_id: int | None = None,
        effort: str | None = None,
        attachment_ids: list[int] | None = None,
        stopping: bool = False,
    ) -> Run:
        if not agent.enabled and not stopping:
            raise ServiceError(f"Agent '{agent.name}' is disabled")
        run = Run(
            project_id=agent.project_id,
            agent_id=agent.id,
            trigger=trigger,
            input_text=text,
            task_id=task_id,
            assignment_id=assignment_id,
            schedule_id=schedule_id,
            source_agent_id=source_agent_id,
            parent_run_id=parent_run_id,
            effort=effort,
            attachment_ids=attachment_ids or [],
            stop_requested_at=utcnow() if stopping else None,
        )
        s.add(run)
        await s.commit()
        bus.publish(agent.project_id, "run.upserted", RunOut.model_validate(run))
        await agent_service.publish(s, agent)
        self._wake.set()
        return run

    def recheck(self) -> None:
        """Look at the waiting turns again, e.g. after the number of parallel agents changed."""
        self._wake.set()

    async def chat(self, s: AsyncSession, agent: Agent, text: str, attachment_ids: list[int] | None = None) -> Run:
        if attachment_ids and len(await board_service.chat_attachments(s, agent.project_id, attachment_ids)) != len(set(attachment_ids)):
            raise ServiceError("Unknown attachment")
        run = await self._join_waiting_chat(s, agent, text, attachment_ids) or await self.enqueue(s, agent, RunTrigger.CHAT, text, attachment_ids=attachment_ids)
        # A working agent reads the message after its current step instead of after its whole turn.
        # Nothing is interrupted, and the message stays queued as its next turn.
        busy_run = await s.scalar(select(Run.id).where(Run.agent_id == agent.id, Run.status == RunStatus.RUNNING).limit(1))
        if busy_run and (text.strip() or attachment_ids):
            await self.runner.notify_run(busy_run, prompts.chat_notice(text, len(attachment_ids or [])))
            if ended := self._end_waits(busy_run, "the user wrote to you"):  # no wait is worth more than the user
                await flow_log.record(s, agent.project_id, "wait.ended", agent_id=agent.id, run_id=busy_run, waits=ended, by="user")
        return run

    async def _wait_in_turn(self, run_id: int | None, kind: str, future: asyncio.Future[str], seconds: float) -> str:
        """Wait in a turn for a result. Raises TimeoutError, or WaitEnded when someone needs the agent now.
        Either way the future is left alone: what the turn waited for goes on."""
        ended: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        waits = self._turn_waits.setdefault(run_id, []) if run_id is not None else []
        waits.append((kind, ended))
        if kind == "job":
            self._wake.set()  # its slot is free while it waits: someone else can start
        try:
            await asyncio.wait({future, ended}, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waits.remove((kind, ended))
            if not waits:
                self._turn_waits.pop(run_id, None)
        if future.done():
            return future.result()
        if ended.done():
            raise WaitEnded(ended.result())
        raise TimeoutError

    def waiting_for_job(self) -> set[int]:
        """Turns that do nothing but wait for a job right now. The limit is about agents that work
        (it protects the token quota), so they take no slot: another agent starts meanwhile. When
        the job ends they go on at once, which can put one more agent to work than the limit for
        the rest of that turn; nothing new starts until that is over."""
        return {run_id for run_id, waits in self._turn_waits.items() if any(kind == "job" for kind, _ in waits)}

    def _end_waits(self, run_id: int, reason: str, *, asks: bool = True) -> list[str]:
        """End what the run's turn is waiting for, so that its agent gets to the message it was just sent.
        Returns what was ended (job, ask)."""
        done = []
        for kind, ended in self._turn_waits.get(run_id, ()):
            if (asks or kind == "job") and not ended.done():
                ended.set_result(reason)
                done.append(kind)
        return done

    async def _join_waiting_chat(self, s: AsyncSession, agent: Agent, text: str, attachment_ids: list[int] | None) -> Run | None:
        """Add a message to the agent's chat run that has not started yet, so it answers everything at once."""
        waiting = await s.scalar(
            select(Run).where(Run.agent_id == agent.id, Run.trigger == RunTrigger.CHAT, Run.status == RunStatus.QUEUED).order_by(Run.id.desc()).limit(1)
        )
        if waiting is None or not agent.enabled:
            return None
        joined = chr(10).join(part for part in (waiting.input_text + chr(10), text) if part.strip())
        # The status check matters: the dispatcher may start this run at any moment.
        result = await s.execute(
            update(Run)
            .where(Run.id == waiting.id, Run.status == RunStatus.QUEUED)
            .values(input_text=joined, attachment_ids=[*waiting.attachment_ids, *(attachment_ids or [])])
        )
        await s.commit()
        if not result.rowcount:
            return None
        await s.refresh(waiting)
        bus.publish(agent.project_id, "run.upserted", RunOut.model_validate(waiting))
        self._wake.set()
        return waiting

    async def withdraw_chat(self, s: AsyncSession, run_id: int) -> None:
        """Delete a chat message that the agent has not received yet (its run has not started)."""
        run = await s.get(Run, run_id)
        if run is None:
            raise NotFound("Message not found")
        project_id, agent_id, attachment_ids = run.project_id, run.agent_id, list(run.attachment_ids)
        # The status check matters: the dispatcher may start this run at any moment.
        result = await s.execute(sql_delete(Run).where(Run.id == run_id, Run.trigger == RunTrigger.CHAT, Run.status == RunStatus.QUEUED))
        await s.commit()
        if not result.rowcount:
            raise ServiceError("The agent has already received this message", 409)
        for attachment in await board_service.chat_attachments(s, project_id, attachment_ids):
            await board_service.delete_attachment(s, attachment)
        bus.publish(project_id, "run.deleted", {"id": run_id, "agent_id": agent_id})
        await agent_service.publish_by_id(s, agent_id)
        # Updates that waited for this turn still need one.
        project = await s.get(Project, project_id)
        if project and await inbox.pending_count(s, agent_id):
            await self._maybe_wake(s, project, agent_id)

    async def hand_ticket(self, s: AsyncSession, task: Task) -> Run:
        """Give a ticket to the project manager, who plans and delegates it."""
        manager = await agent_service.get_manager(s, task.project_id)
        busy = await s.scalar(
            select(Run.id).where(Run.agent_id == manager.id, Run.task_id == task.id, Run.trigger == RunTrigger.TICKET, Run.status.in_((RunStatus.QUEUED, RunStatus.RUNNING)))
        )
        if busy:
            raise ServiceError(f"Ticket #{task.number} is already with the manager")
        if task.status in (TaskStatus.BACKLOG, TaskStatus.TODO):
            await board_service.update_task(s, task, TaskUpdate(status="in_progress"))
        brief = await board_service.ticket_text(s, task)
        run = await self.enqueue(s, manager, RunTrigger.TICKET, prompts.ticket_handoff(brief), task_id=task.id)
        await board_service.publish_task(s, task)
        return run

    async def delegate(
        self,
        s: AsyncSession,
        caller: Agent | None,
        target: Agent,
        title: str,
        instructions: str,
        task: Task | None,
        effort: str | None = None,
        landing_id: int | None = None,
        train_id: int | None = None,
    ) -> Assignment:
        if task is not None and task.project_id != target.project_id:
            raise ServiceError("Ticket belongs to another project")
        if task is not None and task.status in (TaskStatus.BACKLOG, TaskStatus.TODO, TaskStatus.REVIEW):
            await board_service.update_task(s, task, TaskUpdate(status="in_progress"))
        assignment = Assignment(
            project_id=target.project_id,
            task_id=task.id if task else None,
            agent_id=target.id,
            created_by_agent_id=caller.id if caller else None,
            title=title,
            instructions=instructions,
            effort=effort,
            landing_id=landing_id,
            train_id=train_id,
        )
        s.add(assignment)
        await s.commit()
        from_name = caller.name if caller else "PlainBoard" if landing_id or train_id else "the user"
        run = await self.enqueue(
            s,
            target,
            RunTrigger.ASSIGNMENT,
            prompts.assignment_prompt(assignment, from_name, task),
            task_id=assignment.task_id,
            assignment_id=assignment.id,
            source_agent_id=caller.id if caller else None,
            effort=effort,
        )
        assignment.run_id = run.id
        await s.commit()
        bus.publish(target.project_id, "assignment.upserted", AssignmentOut.model_validate(assignment))
        await board_service.publish_task_id(s, assignment.task_id)
        return assignment

    async def message(self, s: AsyncSession, caller: Agent, target: Agent, text: str) -> None:
        """A message is an update for the target's next turn, not a turn of its own: several arrive
        together, and a turn that is about to start anyway brings them along."""
        if caller.id == target.id:
            raise ServiceError("You cannot message yourself")
        if not target.enabled:
            raise ServiceError(f"Agent '{target.name}' is disabled")
        await inbox.push(s, target.id, prompts.message_update(caller, text))
        await self.request_wake(s, target.id)

    async def cancel_assignment(self, s: AsyncSession, assignment: Assignment) -> bool:
        """Cancel an assignment, also one that has no turn right now because it waits for its job."""
        if assignment.status not in (AssignmentStatus.QUEUED, AssignmentStatus.RUNNING):
            return False
        active = list(await s.scalars(select(Run.id).where(Run.assignment_id == assignment.id, Run.status.in_(ACTIVE_RUN_STATUSES))))
        for job in await job_service.running_for_assignment(s, assignment.id):
            await self.stop_job(s, job, settle=False)
        if active:  # the end of its turn closes it
            for run_id in active:
                await self.cancel_run(run_id)
            return True
        assignment.status, assignment.result, assignment.finished_at = AssignmentStatus.CANCELLED, "Cancelled", utcnow()
        await s.commit()
        bus.publish(assignment.project_id, "assignment.upserted", AssignmentOut.model_validate(assignment))
        await board_service.publish_task_id(s, assignment.task_id)
        await agent_service.publish_by_id(s, assignment.agent_id)
        self._wake.set()  # the agent's next assignment may start now
        return True

    async def _held_assignment(self, s: AsyncSession, agent_id: int, *, except_run_id: int | None = None) -> Assignment | None:
        """The agent's assignment that is open but has no turn: it waits for its job or for its next turn."""
        for assignment in await s.scalars(select(Assignment).where(Assignment.agent_id == agent_id, Assignment.status == AssignmentStatus.RUNNING).order_by(Assignment.id)):
            turns = select(Run.id).where(Run.assignment_id == assignment.id, Run.status.in_(ACTIVE_RUN_STATUSES))
            if except_run_id is not None:
                turns = turns.where(Run.id != except_run_id)
            if not await s.scalar(turns.limit(1)):
                return assignment
        return None

    async def _settle(self, s: AsyncSession, agent_id: int) -> None:
        """Make sure an open assignment that waits for nothing any more gets the turn that finishes it."""
        held = await self._held_assignment(s, agent_id)
        if held is None or await job_service.running_for_assignment(s, held.id):
            return
        if await s.scalar(select(Run.id).where(Run.agent_id == agent_id, Run.trigger == RunTrigger.WAKE, Run.status == RunStatus.QUEUED).limit(1)):
            return  # that turn takes it up
        agent = await s.get(Agent, agent_id)
        if agent and agent.enabled:
            await self.enqueue(s, agent, RunTrigger.WAKE, "", assignment_id=held.id, task_id=held.task_id)

    async def ask(self, ctx: RunContext, caller: Agent, target: Agent, question: str) -> str:
        if caller.id == target.id:
            raise ServiceError("You cannot ask yourself")
        if await self._depth(ctx.run_id) >= MAX_ASK_DEPTH:
            raise ServiceError("Too many nested questions; answer with what you know or use message_agent")
        if self._would_deadlock(caller.id, target.id):
            raise ServiceError(f"{target.name} is waiting on you (directly or indirectly). Use message_agent instead.")
        async with self._sm() as s:
            busy_run = await s.scalar(select(Run.id).where(Run.agent_id == target.id, Run.status == RunStatus.RUNNING).limit(1))
            run = await self.enqueue(
                s, target, RunTrigger.ASK, prompts.ask_prompt(caller, question), source_agent_id=caller.id, parent_run_id=ctx.run_id
            )
        if busy_run:
            await self.runner.notify_run(busy_run, prompts.ask_notice(caller, question))
            if ended := self._end_waits(busy_run, f"{caller.name} is waiting for your answer", asks=False):  # a job's result comes by itself
                async with self._sm() as s:
                    await flow_log.record(s, target.project_id, "wait.ended", agent_id=target.id, run_id=busy_run, waits=ended, by=caller.name)
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._waiters[run.id] = future
        self._add_wait(caller.id, target.id)
        try:
            return await self._wait_in_turn(ctx.run_id, "ask", future, ASK_BUSY_TIMEOUT_SECONDS if busy_run else ASK_TIMEOUT_SECONDS)
        except (TimeoutError, WaitEnded) as exc:
            # Nobody waits any more: the answer becomes an update.
            self._waiters.pop(run.id, None)
            self._orphan_asks.add(run.id)
            why = f"Stopped waiting for {target.name}: {exc}." if isinstance(exc, WaitEnded) else f"{target.name} has not answered yet."
            return f"{why} The answer will reach you as an update in a later turn."
        finally:
            self._remove_wait(caller.id, target.id)

    async def request_wake(self, s: AsyncSession, agent_id: int) -> None:
        """Queue one turn to process inbox updates, unless a queued run will consume them anyway."""
        if await s.scalar(select(Run.id).where(Run.agent_id == agent_id, Run.status == RunStatus.QUEUED)):
            return
        agent = await s.get(Agent, agent_id)
        if agent and agent.enabled:
            await self.enqueue(s, agent, RunTrigger.WAKE, "")

    async def cancel_run(self, run_id: int) -> bool:
        async with self._sm() as s:
            run = await s.get(Run, run_id)
            if run is None or run.status not in (RunStatus.QUEUED, RunStatus.RUNNING):
                return False
            if run.status == RunStatus.QUEUED:
                run.status, run.finished_at, run.error = RunStatus.CANCELLED, utcnow(), "Cancelled before start"
                await s.commit()
                await self._after_run(s, run)
                bus.publish(run.project_id, "run.upserted", RunOut.model_validate(run))
                await agent_service.publish_by_id(s, run.agent_id)
                return True
        self._cancel_requested.add(run_id)
        await self.runner.cancel(run_id)
        asyncio.get_running_loop().call_later(CANCEL_GRACE_SECONDS, self._force_cancel, run_id)
        return True

    def refresh_limits(self) -> None:
        """Ask for the plan limits in the background, at most every five minutes, and only accounts
        that have not just reported them. Uses no tokens, or a handful (see services/limits.py)."""
        if self._limits_task and not self._limits_task.done() or (self._limits_asked and time.monotonic() - self._limits_asked < 300):
            return
        self._limits_asked = time.monotonic()
        self._limits_task = asyncio.create_task(self._refresh_limits(), name="limits-refresh")

    async def _refresh_limits(self) -> None:
        async with self._sm() as s:
            values = await settings_service.get_all(s)
            reported = await limits_service.fresh(s)  # an account in use reports with every turn
            accounts = [a for a in await account_service.list_accounts(s) if a.enabled and account_service.has_plan(a) and a.key not in reported]
            found = await asyncio.gather(
                *(self.runner.limits(a.provider, settings_service.credentials(values, account=account_service.fields(a))) for a in accounts)
            )
            for account, report in zip(accounts, found):
                if report.windows:
                    await limits_service.record(s, account.key, report.windows, complete=report.complete)

    async def wind_down(self, s: AsyncSession, agent: Agent, reason: str = "") -> bool:
        """Let an agent finish safely: drop what it has not started and tell its running turn to wrap up.
        An agent that only has background jobs left gets a turn for that, even if it is paused.
        A paused (disabled) agent keeps its assignments: they go on when it is enabled again (``resume``).
        Returns whether the agent is still wrapping up."""
        rows = (await s.execute(select(Run.id, Run.status, Run.assignment_id).where(Run.agent_id == agent.id, Run.status.in_((RunStatus.QUEUED, RunStatus.RUNNING))))).all()
        running = False
        for run_id, status, assignment_id in rows:
            if status == RunStatus.QUEUED:
                if agent.enabled or assignment_id is None:
                    await self.cancel_run(run_id)
            elif await self.runner.notify_run(run_id, prompts.wind_down_notice(reason), urgent=True):
                running = True
                run = await s.get(Run, run_id)
                run.stop_requested_at = run.stop_requested_at or utcnow()
                await s.commit()
                bus.publish(run.project_id, "run.upserted", RunOut.model_validate(run))
        if not running and (jobs := [job for job in await job_service.running(s) if job.agent_id == agent.id]):
            await self.enqueue(s, agent, RunTrigger.WAKE, prompts.wind_down_jobs_prompt(reason, [job_service.label(job) for job in jobs]), stopping=True)
            running = True
        await flow_log.record(s, agent.project_id, "agent.wound_down", agent_id=agent.id, paused=not agent.enabled, turns=len(rows), still_running=running, reason=reason)
        return running

    async def resume(self, s: AsyncSession, agent: Agent) -> None:
        """An agent was enabled again: what it had open when it was paused goes on by itself."""
        held = await self._held_assignment(s, agent.id)
        await flow_log.record(s, agent.project_id, "agent.resumed", agent_id=agent.id, assignment_id=held.id if held else None)
        if held:
            await inbox.push(s, agent.id, prompts.RESUME_NOTE)
            await self._settle(s, agent.id)
        self._wake.set()  # assignments that waited in the queue

    async def stop_agent(self, s: AsyncSession, agent: Agent) -> int:
        """Stop everything an agent is doing or about to do, at once: runs and background jobs."""
        run_ids = (await s.scalars(select(Run.id).where(Run.agent_id == agent.id, Run.status.in_((RunStatus.QUEUED, RunStatus.RUNNING))))).all()
        stopped = 0
        for run_id in run_ids:
            stopped += await self.cancel_run(run_id)
        # Also what has no turn right now: an assignment that waits for its job.
        for assignment in list(await s.scalars(select(Assignment).where(Assignment.agent_id == agent.id, Assignment.status == AssignmentStatus.RUNNING))):
            await self.cancel_assignment(s, assignment)
        for job in await job_service.running(s):
            if job.agent_id == agent.id:
                stopped += await self.stop_job(s, job, settle=False)
        return stopped

    def _force_cancel(self, run_id: int) -> None:
        task = self._running.get(run_id)
        if task and not task.done():
            task.cancel()

    # ------------------------------------------------------------------ deadlock bookkeeping

    async def _depth(self, run_id: int) -> int:
        depth = 0
        async with self._sm() as s:
            current = await s.get(Run, run_id)
            while current and current.parent_run_id and depth <= MAX_ASK_DEPTH:
                depth += 1
                current = await s.get(Run, current.parent_run_id)
        return depth

    def _would_deadlock(self, caller_id: int, target_id: int) -> bool:
        stack, seen = [target_id], set()
        while stack:
            node = stack.pop()
            if node == caller_id:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(self._waits_for.get(node, {}))
        return False

    def _add_wait(self, caller_id: int, target_id: int) -> None:
        edges = self._waits_for.setdefault(caller_id, {})
        edges[target_id] = edges.get(target_id, 0) + 1

    def _remove_wait(self, caller_id: int, target_id: int) -> None:
        edges = self._waits_for.get(caller_id, {})
        if edges.get(target_id, 0) <= 1:
            edges.pop(target_id, None)
        else:
            edges[target_id] -= 1

    # ------------------------------------------------------------------ dispatch

    async def _dispatch_loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            try:
                await self._dispatch_once()
            except Exception:
                log.exception("Dispatcher error")

    async def _dispatch_once(self) -> None:
        async with self._sm() as s:
            configured = await settings_service.get(s, "max_parallel_runs")
            max_parallel = 3 if configured is None else int(configured)  # 0 pauses the members
            queued = (await s.scalars(select(Run).where(Run.status == RunStatus.QUEUED).order_by(Run.id))).all()
            managers = set(await s.scalars(select(Agent.id).where(Agent.is_manager))) if queued else set()
            # A paused agent keeps its assignments in the queue; only its turn to wrap up still runs.
            paused = set(await s.scalars(select(Agent.id).where(~Agent.enabled))) if queued else set()
            # An agent finishes its open assignment before it starts another one.
            occupied = set(await s.scalars(select(Assignment.agent_id).where(Assignment.status == AssignmentStatus.RUNNING))) if queued else set()
            repairs = Assignment.landing_id.is_not(None) | Assignment.train_id.is_not(None)
            fixes = set(await s.scalars(select(Assignment.id).where(Assignment.status == AssignmentStatus.QUEUED, repairs))) if queued else set()

            def order(run: Run) -> tuple[int, int]:
                """Someone waiting for an answer goes first, then what the user wrote, then work that
                has been started (a job's result, a branch to repair for landing), then new work."""
                if run.parent_run_id is not None:
                    return 0, run.id
                if run.trigger == RunTrigger.CHAT:
                    return 1, run.id
                if run.trigger == RunTrigger.WAKE or run.assignment_id in fixes:
                    return 2, run.id
                return 3, run.id

            candidates = [
                (r.id, r.agent_id, r.parent_run_id is not None, r.agent_id in managers)
                for r in sorted(queued, key=order)
                if not (r.trigger == RunTrigger.ASSIGNMENT and r.agent_id in occupied) and not (r.agent_id in paused and r.assignment_id and not r.stop_requested_at)
            ]
        for run_id, agent_id, nested, manager in candidates:
            if agent_id in self._busy_agents or run_id in self._running:
                continue
            # The limit is about members. A manager's turn never waits for a slot, not even at limit 0:
            # the user gets an answer, and the members' reports get read.
            unlimited = nested or manager
            if not unlimited and len(self._running.keys() - self._unlimited - self.waiting_for_job()) >= max_parallel:
                continue
            self._busy_agents.add(agent_id)
            if unlimited:
                self._unlimited.add(run_id)
            self._running[run_id] = asyncio.create_task(self._execute(run_id, agent_id), name=f"run-{run_id}")

    # ------------------------------------------------------------------ execution

    async def _execute(self, run_id: int, agent_id: int) -> None:
        ctx: RunContext | None = None
        try:
            prepared = await self._prepare(run_id)
            if prepared is None:
                return
            spec, ctx = prepared
            outcome = await self._stream(spec, ctx)
            await self._finish(run_id, outcome)
        except asyncio.CancelledError:
            await asyncio.shield(self._finish(run_id, Outcome(error="Run cancelled", cancelled=True)))
            raise
        except Exception as exc:
            log.exception("Run %s failed", run_id)
            await self._finish(run_id, Outcome(error=str(exc)[:4000]))
        finally:
            if ctx:
                self._tokens.pop(ctx.token, None)
            self._running.pop(run_id, None)
            self._unlimited.discard(run_id)
            self._cancel_requested.discard(run_id)
            self._job_waits.pop(run_id, None)
            self._busy_agents.discard(agent_id)
            self._wake.set()
            try:
                async with self._sm() as s:
                    await self._settle(s, agent_id)
                    await agent_service.publish_by_id(s, agent_id)
            except Exception:
                log.exception("Could not publish agent state")

    async def _prepare(self, run_id: int) -> tuple[RunSpec, RunContext] | None:
        async with self._sm() as s:
            run = await s.get(Run, run_id)
            if run is None or run.status != RunStatus.QUEUED:
                return None
            agent = await s.get(Agent, run.agent_id)
            project = await s.get(Project, run.project_id)
            assert agent and project
            if not agent.enabled and not run.stop_requested_at:  # disabled while the run was waiting
                run.status, run.finished_at, run.error = RunStatus.CANCELLED, utcnow(), "Agent is disabled"
                await s.commit()
                await self._after_run(s, run)
                bus.publish(run.project_id, "run.upserted", RunOut.model_validate(run))
                return None
            # A wake-up while an assignment is open continues that assignment: its end decides whether
            # the assignment is finished, and its final message is the report then.
            held = await self._held_assignment(s, agent.id, except_run_id=run.id) if run.trigger == RunTrigger.WAKE else None
            if run.trigger == RunTrigger.WAKE and not run.input_text and not await inbox.pending_count(s, agent.id):
                if held is None or await job_service.running_for_assignment(s, held.id):
                    # Another turn that went first has already processed the updates this wake-up was for.
                    await s.delete(run)
                    await s.commit()
                    bus.publish(project.id, "run.deleted", {"id": run_id, "agent_id": agent.id})
                    return None
            if held is not None:
                run.assignment_id, run.task_id = held.id, held.task_id
            conv = await agent_service.conversation_for(s, agent.id)
            run.status, run.started_at = RunStatus.RUNNING, utcnow()
            await s.commit()
            await s.refresh(run)  # a chat message may have joined until now

            updates = await inbox.consume(s, agent.id)
            self._job_news.discard(agent.id)
            files = await board_service.chat_attachments(s, project.id, run.attachment_ids) if run.attachment_ids else []
            if run.input_text or files:
                text = run.input_text
            elif held is not None and not await job_service.running_for_assignment(s, held.id):
                text = prompts.continue_prompt(held, (await timing_service.ledger(s, held)).text())
            else:
                text = prompts.wake_prompt(agent.is_manager)
            meta: dict = {"trigger": run.trigger}
            if run.task_id:
                meta["task_id"] = run.task_id
            if run.assignment_id:
                meta["assignment_id"] = run.assignment_id
            if run.source_agent_id:
                meta["source_agent_id"] = run.source_agent_id
            if updates:
                meta["inbox"] = updates
            if files:
                meta["attachments"] = [{"id": a.id, "filename": a.filename, "content_type": a.content_type, "path": board_service.runner_path(a)} for a in files]
            message = Message(conversation_id=conv.id, role="user", kind="text", content=text, meta=meta, run_id=run.id)
            if run.trigger == RunTrigger.CHAT:
                # A busy agent read it when it was sent and may have reacted before this turn: the chat
                # shows it at that time, above those replies.
                message.created_at = run.created_at
            s.add(message)
            assignment = await s.get(Assignment, run.assignment_id) if run.assignment_id else None
            if assignment:
                assignment.status = AssignmentStatus.RUNNING
            await s.commit()
            bus.publish(project.id, "message.created", MessageOut.model_validate(message))
            bus.publish(project.id, "run.upserted", RunOut.model_validate(run))
            if assignment:
                bus.publish(project.id, "assignment.upserted", AssignmentOut.model_validate(assignment))
            await agent_service.publish(s, agent)
            await board_service.publish_task_id(s, run.task_id)

            values = await settings_service.get_all(s)
            board = ""
            if agent.is_manager:
                limit = int(values["max_parallel_runs"])
                capacity = await board_service.capacity(s, project.id, limit, except_run_id=run.id, waiting=self.waiting_for_job())
                team = [a for a in await agent_service.list_for_project(s, project.id) if a.enabled and not a.is_manager]
                open_todos = await todo_service.open_stats(s, [a.id for a in team])
                todo_counts = [(a.name, *_count_and_age(open_todos.get(a.id))) for a in team[:12]]
                checks = (
                    prompts.in_flight_block(*await board_service.in_flight(s, project.id)),
                    prompts.board_check(await board_service.idle_in_progress(s, project.id)),
                    prompts.review_line(await board_service.unmarked_in_review(s, project.id)),
                    prompts.capacity_line(capacity),
                    prompts.team_todos_line(todo_counts),
                    timing_service.waiting_line(await timing_service.cached_overview(s, project.id, 24)),
                )
                board = "\n\n".join(c for c in checks if c)
            elif run.trigger == RunTrigger.ASSIGNMENT and assignment:
                # Context belongs to the piece of work: another ticket starts without the earlier conversation.
                topic = f"task:{assignment.task_id}" if assignment.task_id else f"assignment:{assignment.id}"
                fresh = conv.session_topic != topic
                if fresh:
                    marker = await self.context.new_topic(s, conv, topic, before_id=message.id)
                    if marker:
                        bus.publish(project.id, "message.created", MessageOut.model_validate(marker))
                earlier = []
                if fresh:
                    done = select(Assignment).where(Assignment.agent_id == agent.id, Assignment.id != assignment.id, Assignment.finished_at.is_not(None))
                    earlier = list(await s.scalars(done.order_by(Assignment.id.desc()).limit(3)))
                facts = timing_service.facts_text(await timing_service.cached_overview(s, project.id, timing_service.FACTS_DAYS * 24))
                board = prompts.assignment_opening(facts, earlier, fresh)
            full_text = prompts.with_preface(prompts.with_attachments(text, meta.get("attachments")), now=prompts.now_line(values["timezone"]), inbox=updates, board=board)
            turn = await self.context.build_turn(s, project=project, agent=agent, conv=conv, text=full_text, message_id=message.id)
            if turn.briefing_changes:
                message.meta = {**message.meta, "briefing_update": turn.briefing_changes}
                await s.commit()
                bus.publish(project.id, "message.updated", MessageOut.model_validate(message))
            secret_env = secret_service.env(await secret_service.effective(s, project.id))
            account = await account_service.pick(s, agent.provider, agent, conv)
            if account:
                conv.account_key, run.account_key = account.key, account.key
                await s.commit()
            ctx = RunContext(
                run_id=run.id,
                project_id=project.id,
                agent_id=agent.id,
                token=secrets.token_urlsafe(32),
                redact=secret_service.redactor(secret_env),
                account_key=account.key if account else None,
                assignment_id=run.assignment_id,
                task_id=run.task_id,
            )
            self._tokens[ctx.token] = ctx
            spec = RunSpec(
                run_id=str(run.id),
                provider=agent.provider,
                model=agent.model,
                effort=run.effort or agent.effort or None,
                toolset=agent.toolset,  # type: ignore[arg-type]
                system_prompt=turn.system_prompt,
                prompt=turn.prompt,
                resume_session_id=turn.resume_session_id,
                max_turns=agent.max_turns,
                workspace=WorkspaceSpec(
                    path=workspace_path(project, agent),
                    repos=await repo_service.specs_for_agent(s, agent),
                    shared=bool(project.workspace_path),
                    setup_script=project.setup_script,
                ),
                credentials=settings_service.credentials(values, project_service.github_token(project), secret_env, account_service.fields(account)),
                bridge=ToolBridgeSpec(
                    base_url=get_config().internal_url, run_id=str(run.id), run_token=ctx.token, tools=registry.tool_defs_for(agent)
                ),
                mcp_servers=await mcp_service.specs_for_agent(s, agent),
            )
            return spec, ctx

    async def _stream(self, spec: RunSpec, ctx: RunContext) -> Outcome:
        outcome = Outcome(resumed=spec.resume_session_id is not None)
        fresh_session = spec.resume_session_id is None
        async with self._sm() as s:
            conv = await agent_service.conversation_for(s, ctx.agent_id)
            base = {"agent_id": ctx.agent_id, "conversation_id": conv.id, "run_id": ctx.run_id}
            async for event in self.runner.stream_run(spec):
                if event.type == "text_delta":
                    bus.publish(ctx.project_id, "message.delta", {**base, "text": ctx.redact(event.text)})
                elif event.type == "session":
                    conv.provider_session_id = event.session_id
                    await s.commit()
                elif event.type == "assistant_text":
                    outcome.last_text = event.text
                    await self._add_message(s, ctx, conv.id, role="assistant", kind="text", content=ctx.redact(event.text))
                elif event.type == "tool_use":
                    meta = {"name": event.name, "tool_use_id": event.id, "input": truncate(ctx.redact(json.dumps(event.input, ensure_ascii=False)), 1500)}
                    msg = await self._add_message(s, ctx, conv.id, role="assistant", kind="tool", content=event.name, meta=meta)
                    outcome.tool_messages[event.id] = msg.id
                elif event.type == "tool_result":
                    msg_id = outcome.tool_messages.get(event.tool_use_id)
                    if msg_id and (msg := await s.get(Message, msg_id)):
                        msg.meta = {**msg.meta, "result": ctx.redact(event.content), "is_error": event.is_error}
                        await s.commit()
                        bus.publish(ctx.project_id, "message.updated", MessageOut.model_validate(msg))
                elif event.type == "usage":
                    if fresh_session:  # first call of a new session = fixed base context
                        conv.session_base_tokens = event.context_tokens
                        fresh_session = False
                    conv.context_tokens = event.context_tokens
                    await s.commit()
                    bus.publish(ctx.project_id, "conversation.updated", {"agent_id": ctx.agent_id, "context_tokens": event.context_tokens})
                elif event.type == "limits" and ctx.account_key:
                    await limits_service.record(s, ctx.account_key, event.windows, complete=event.complete)
                elif event.type == "compaction":
                    marker = self.context.provider_compacted(conv, event.tokens_before)
                    marker.run_id = ctx.run_id
                    s.add(marker)
                    await s.commit()
                    bus.publish(ctx.project_id, "message.created", MessageOut.model_validate(marker))
                elif event.type == "result":
                    outcome.result = event
                elif event.type == "error":
                    outcome.error, outcome.cancelled = event.message, event.cancelled
        return outcome

    async def _add_message(self, s: AsyncSession, ctx: RunContext, conv_id: int, **fields) -> Message:
        msg = Message(conversation_id=conv_id, run_id=ctx.run_id, **fields)
        s.add(msg)
        await s.commit()
        bus.publish(ctx.project_id, "message.created", MessageOut.model_validate(msg))
        return msg

    async def _finish(self, run_id: int, outcome: Outcome) -> None:
        async with self._sm() as s:
            run = await s.get(Run, run_id)
            if run is None or run.status not in (RunStatus.RUNNING, RunStatus.QUEUED):
                return
            agent = await s.get(Agent, run.agent_id)
            conv = await s.scalar(select(Conversation).where(Conversation.agent_id == run.agent_id))
            res = outcome.result
            if outcome.cancelled or run_id in self._cancel_requested:
                run.status = RunStatus.CANCELLED
                run.error = "Cancelled"
            elif outcome.error or res is None or res.is_error:
                run.status = RunStatus.FAILED
                run.error = outcome.error or (res.error if res else None) or "Run ended without a result"
            else:
                run.status = RunStatus.SUCCEEDED
            run.result_text = (res.text if res and res.text else None) or outcome.last_text
            run.finished_at = utcnow()
            run.job_wait_seconds = int(self._job_waits.pop(run_id, 0))
            if res:
                run.input_tokens, run.output_tokens = res.input_tokens, res.output_tokens
                run.cache_read_tokens, run.cache_creation_tokens = res.cache_read_tokens, res.cache_creation_tokens
                run.cost_usd, run.num_turns, run.context_tokens = res.cost_usd, res.num_turns, res.context_tokens
                if res.session_cost_usd is not None and conv:
                    before = conv.session_cost_usd if outcome.resumed else 0.0
                    run.cost_usd = res.session_cost_usd - before if res.session_cost_usd >= before else res.session_cost_usd
                    conv.session_cost_usd = res.session_cost_usd
            if conv:
                conv.total_input_tokens += run.input_tokens + run.cache_read_tokens + run.cache_creation_tokens
                conv.total_output_tokens += run.output_tokens
                conv.total_cost_usd += run.cost_usd
                if res and res.session_id:
                    conv.provider_session_id = res.session_id
                if res and res.context_tokens:
                    conv.context_tokens = res.context_tokens
                if run.status == RunStatus.FAILED and _session_lost(run.error):
                    conv.provider_session_id = None
            await s.commit()
            if run.status != RunStatus.SUCCEEDED and conv:
                await self._add_message(
                    s, RunContext(run.id, run.project_id, run.agent_id, ""), conv.id, role="system", kind="error", content=run.error or "Failed"
                )
            bus.publish(run.project_id, "run.upserted", RunOut.model_validate(run))
            try:
                await self._after_run(s, run)
            except Exception:
                log.exception("Post-run hook failed for run %s", run_id)
            if agent and conv and run.status == RunStatus.SUCCEEDED:
                try:
                    await self.context.maybe_compress(s, agent, conv)
                except Exception as exc:
                    log.exception("Compression failed for agent %s", agent.id)
                    await self._add_message(
                        s, RunContext(run.id, run.project_id, run.agent_id, ""), conv.id, role="system", kind="error", content=f"Context compression failed: {exc}"
                    )

    async def _after_run(self, s: AsyncSession, run: Run) -> None:
        """Route results: answers to waiting askers, reports to requesters, comments to tickets."""
        ok = run.status == RunStatus.SUCCEEDED
        text = run.result_text or ""
        agent = await s.get(Agent, run.agent_id)
        project = await s.get(Project, run.project_id)
        if agent is None or project is None:
            return
        outcome_text = truncate(text, 2000) if ok else f"failed: {run.error}"

        if run.trigger == RunTrigger.ASK:
            future = self._waiters.pop(run.id, None)
            answer = text if ok else f"{agent.name} could not answer: {run.error}"
            if future and not future.done():
                future.set_result(answer or "(no answer)")
            elif run.id in self._orphan_asks and run.source_agent_id:
                self._orphan_asks.discard(run.id)
                await inbox.push(s, run.source_agent_id, f"Late answer from {agent.name}: {truncate(answer, 2000)}")
                await self._maybe_wake(s, project, run.source_agent_id)

        elif run.assignment_id and run.trigger in (RunTrigger.ASSIGNMENT, RunTrigger.WAKE):
            assignment = await s.get(Assignment, run.assignment_id)
            if assignment and assignment.status in (AssignmentStatus.QUEUED, AssignmentStatus.RUNNING):
                await self._assignment_turn_ended(s, run, assignment, agent, project, outcome_text)

        elif run.trigger == RunTrigger.TICKET and not ok and run.task_id:
            ticket = await s.get(Task, run.task_id)
            if ticket:
                await board_service.add_comment(s, ticket, f"Manager could not process the ticket: {run.error}", author_kind="system")

        # Any other turn's final message reaches nobody: what someone needs to know is sent explicitly
        # (message_agent), so no turn is spent on an update nobody asked for.
        await board_service.publish_task_id(s, run.task_id)

    async def _waits_for_job(self, s: AsyncSession, assignment: Assignment) -> bool:
        """Whether an assignment has a job running, or a job result its agent has not read yet."""
        if await job_service.running_for_assignment(s, assignment.id):
            return True
        return assignment.agent_id in self._job_news and bool(await inbox.pending_count(s, assignment.agent_id))

    async def _assignment_turn_ended(self, s: AsyncSession, run: Run, assignment: Assignment, agent: Agent, project: Project, outcome_text: str) -> None:
        """Close the assignment and send its one report, unless it still waits for a job of its own:
        then it stays open, and the turn that the job's result starts continues it."""
        ok = run.status == RunStatus.SUCCEEDED
        text = run.result_text or ""
        if ok and project.auto_wake_manager and await self._waits_for_job(s, assignment):
            bus.publish(project.id, "assignment.upserted", AssignmentOut.model_validate(assignment))
            return
        if ok and run.stop_requested_at and not agent.enabled:
            # Paused, not finished: the assignment stays open and goes on when the agent is enabled again.
            bus.publish(project.id, "assignment.upserted", AssignmentOut.model_validate(assignment))
            return
        assignment.status = {
            RunStatus.SUCCEEDED: AssignmentStatus.DONE,
            RunStatus.CANCELLED: AssignmentStatus.CANCELLED,
        }.get(run.status, AssignmentStatus.FAILED)  # type: ignore[call-overload]
        assignment.result = text if ok else run.error
        assignment.finished_at = utcnow()
        await s.commit()
        timing_service.forget_cache()
        took = (await timing_service.ledger(s, assignment)).text()
        bus.publish(project.id, "assignment.upserted", AssignmentOut.model_validate(assignment))
        # The report stays with the assignment, which the ticket shows: it is not repeated as a comment.
        ticket = await s.get(Task, assignment.task_id) if assignment.task_id else None
        if ok and (assignment.landing_id or assignment.train_id):
            return  # repair for landing: PlainBoard puts the branches on the next train, nobody has to read this
        ref = f", ticket #{ticket.number}" if ticket else ""
        recipient = assignment.created_by_agent_id or (await agent_service.get_manager(s, project.id)).id
        if recipient != agent.id:
            still_open = (await todo_service.open_stats(s, [agent.id])).get(agent.id, (0, None))[0]
            todos_note = f" [{agent.name} still has {still_open} open task(s) on its list: check that they are still right.]" if still_open else ""
            await inbox.push(s, recipient, f'Assignment #{assignment.id} "{assignment.title}" ({agent.name}{ref}) {assignment.status} after {took}: {outcome_text}{todos_note}')
            await self._maybe_wake(s, project, recipient)

    async def decision_answered(self, s: AsyncSession, decision: Decision) -> None:
        """Tell the agent that asked (or the manager, if it is gone) what the user decided."""
        project = await s.get(Project, decision.project_id)
        task = await s.get(Task, decision.task_id)
        if project is None or task is None:
            return
        recipient = decision.requested_by_agent_id or (await agent_service.get_manager(s, project.id)).id
        await inbox.push(s, recipient, f'User decided on ticket #{task.number} ("{decision.question}"): {decision_service.answer_summary(decision)}')
        await self._maybe_wake(s, project, recipient)

    async def user_action_done(self, s: AsyncSession, task: Task, action: str, note: str) -> None:
        """Tell the manager that the user did what the ticket was waiting for."""
        project = await s.get(Project, task.project_id)
        manager = await agent_service.get_manager(s, task.project_id)
        await inbox.push(s, manager.id, f'User finished "{action}" on ticket #{task.number}' + (f": {note.strip()}" if note.strip() else "."))
        await self._maybe_wake(s, project, manager.id)

    # ------------------------------------------------------------------ background jobs

    async def start_job(self, s: AsyncSession, project: Project, agent: Agent, command: str, assignment_id: int | None = None) -> Job:
        """``assignment_id``: the assignment that needs this job's result and stays open until it ends."""
        values = await settings_service.get_all(s)
        spec = JobSpec(
            id=secrets.token_hex(8),
            command=command,
            workspace=workspace_path(project, agent),
            credentials=settings_service.credentials(
                values, project_service.github_token(project), secret_service.env(await secret_service.effective(s, project.id))
            ),
        )
        try:
            state = await self.runner.start_job(spec)
        except RunnerError as exc:
            raise ServiceError(f"Could not start the job: {exc}") from exc
        # Only now, so that the watcher never looks for a job the runner has not accepted yet.
        return await job_service.add(s, project.id, agent.id, spec.id, command, state.log_path, assignment_id)

    async def stop_job(self, s: AsyncSession, job: Job, *, settle: bool = True) -> bool:
        try:
            if not await self.runner.stop_job(job.runner_id):
                return False  # already over: the watcher reports it
        except RunnerError as exc:
            raise ServiceError(f"Could not stop the job: {exc}") from exc
        await s.refresh(job)
        if job.status == JobStatus.RUNNING:
            await job_service.finish(s, job, JobStatus.STOPPED)
            if settle and job.assignment_id and job.agent_id not in self._busy_agents:
                # Its assignment waited for it: the agent has to hear that it will not end by itself.
                await inbox.push(s, job.agent_id, f"{job_service.label(job)} was stopped before it ended. Log: {job.log_path}")
                await self._settle(s, job.agent_id)
        return True

    async def wait_job(self, job_id: int, seconds: float, run_id: int | None = None) -> str | None:
        """The job's result if it ends within the time: then it is not sent as an update as well.
        Raises WaitEnded when someone needs the agent before that."""
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._job_waiters[job_id] = future
        started = time.monotonic()
        try:
            return await self._wait_in_turn(run_id, "job", future, seconds)
        except TimeoutError:
            return None
        finally:
            if run_id is not None:  # waiting is not work: the assignment's time is split accordingly
                self._job_waits[run_id] = self._job_waits.get(run_id, 0.0) + time.monotonic() - started
            if self._job_waiters.get(job_id) is future:
                del self._job_waiters[job_id]

    async def job_ended(self, s: AsyncSession, job: Job, state: JobState | None) -> None:
        """Record the end of a job and tell the agent that started it. ``state`` None = job lost."""
        project = await s.get(Project, job.project_id)
        if state is None:
            await job_service.finish(s, job, JobStatus.LOST)
            text = f"{job_service.label(job)} was lost because its container restarted, and may be half done. Check its state and start it again if needed. Log: {job.log_path}"
        else:
            await job_service.finish(s, job, JobStatus.SUCCEEDED if state.exit_code == 0 else JobStatus.FAILED, state.exit_code)
            redact = secret_service.redactor(secret_service.env(await secret_service.effective(s, job.project_id)))
            took = timing_service.duration((job.finished_at - job.created_at).total_seconds())  # the agent has no clock: say it
            text = f"{job_service.label(job)} finished with exit code {state.exit_code} after {took}. Log: {job.log_path}\nLast output:\n{redact(state.tail)}"
        timing_service.forget_cache()
        waiter = self._job_waiters.pop(job.id, None)
        if waiter and not waiter.done():
            waiter.set_result(text)  # its turn is still running and takes the result from here
            return
        await inbox.push(s, job.agent_id, text)
        if job.assignment_id:
            self._job_news.add(job.agent_id)
        if project:
            await self._maybe_wake(s, project, job.agent_id)
            if project.auto_wake_manager and job.agent_id not in self._busy_agents:
                await self._settle(s, job.agent_id)  # a turn already waiting may not be the one that continues the assignment

    async def _maybe_wake(self, s: AsyncSession, project: Project, agent_id: int) -> None:
        if project.auto_wake_manager:
            await self.request_wake(s, agent_id)

    # ------------------------------------------------------------------ tools

    async def execute_tool(self, ctx: RunContext, name: str, args: dict) -> ToolCallResult:
        async with self._sm() as s:
            agent = await s.get(Agent, ctx.agent_id)
            project = await s.get(Project, ctx.project_id)
            if agent is None or project is None:
                return ToolCallResult(text="Agent no longer exists", is_error=True)
            tool_ctx = registry.ToolContext(session=s, project=project, agent=agent, run=ctx, runs=self)
            return await registry.execute(name, tool_ctx, args)


def _session_lost(error: str | None) -> bool:
    text = (error or "").lower()
    return "no conversation found" in text or ("session" in text and "not found" in text)
