"""The models behind a project's ideas (services/ideas.py): sorting them into topics, and planning tickets.

Neither is a turn of a team member: no queue, not counted against the members at work, no tool, nothing
in a chat. Each is one completion on the project's runner and subscriptions, stored as a ``Run`` without
text (trigger ``ideas``, on the manager's account) for the statistics.

- Sorting: when ideas are added, after a short pause (several quick ones go together), with the model of
  the project's questions unless it has one for ideas (``Project.idea_*``). If it fails or answers
  nonsense, the ideas stay unsorted (``sort_state`` failed).
- Planning: a proposal of tickets from chosen ideas, with the manager's model, which is shown the board's
  tickets so it plans none twice. The proposal lives in memory until a person applies it; nothing is
  created before that.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from plainboard_protocol import CompletionResult, CompletionSpec, ProviderInfo
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..events import bus
from ..models import Agent, Idea, IdeaStatus, Project, Run, RunStatus, RunTrigger, utcnow
from ..schemas import IdeaPlanOut, LeftOutIdea, PlannedTicket
from ..services import accounts as account_service
from ..services import agents as agent_service
from ..services import ideas as idea_service
from ..services import settings as settings_service
from ..services.errors import NotFound
from ..services.util import truncate
from . import questions

if TYPE_CHECKING:
    from .run_manager import RunManager

log = logging.getLogger(__name__)

SORT_DELAY = 3.0  # seconds to wait for more ideas before sorting
SORT_BATCH = 60  # ideas sorted with one call; more wait for the next
PLAN_KEEP_SECONDS = 6 * 3600  # an unapplied proposal is forgotten after this
PLANS_PER_PROJECT = 10


@dataclass
class Plan:
    id: str
    project_id: int
    idea_ids: list[int]
    status: str = "running"
    tickets: list[PlannedTicket] = field(default_factory=list)
    left_out: list[LeftOutIdea] = field(default_factory=list)
    error: str = ""
    created_at: datetime = field(default_factory=utcnow)
    made: float = field(default_factory=time.monotonic)

    def out(self) -> IdeaPlanOut:
        return IdeaPlanOut(
            id=self.id, project_id=self.project_id, status=self.status, idea_ids=self.idea_ids, tickets=self.tickets, left_out=self.left_out, error=self.error, created_at=self.created_at
        )  # fmt: skip


class Ideas:
    def __init__(self, runs: RunManager) -> None:
        self.runs = runs
        self._sorters: dict[int, asyncio.Task[None]] = {}
        self._again: set[int] = set()  # more ideas came while sorting
        self._full: set[int] = set()  # sort all open ideas anew
        self._plans: dict[str, Plan] = {}
        self._planners: set[asyncio.Task[None]] = set()

    # --- sorting -------------------------------------------------------------------------------

    def sorting(self, project_id: int) -> bool:
        task = self._sorters.get(project_id)
        return task is not None and not task.done()

    def sort_soon(self, project_id: int, *, full: bool = False) -> None:
        if full:
            self._full.add(project_id)
        if self.sorting(project_id):
            self._again.add(project_id)
            return
        self._sorters[project_id] = asyncio.create_task(self._sort_loop(project_id), name=f"ideas-sort-{project_id}")
        bus.publish(project_id, "ideas.sorting", {"sorting": True})

    async def _sort_loop(self, project_id: int) -> None:
        try:
            await asyncio.sleep(SORT_DELAY)
            while True:
                self._again.discard(project_id)
                full = project_id in self._full
                self._full.discard(project_id)
                more = await self._sort(project_id, full)
                if not more and project_id not in self._again:
                    break
                if project_id in self._again:
                    await asyncio.sleep(SORT_DELAY)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Sorting the ideas of project %s failed", project_id)
        finally:
            if self._sorters.get(project_id) is asyncio.current_task():
                del self._sorters[project_id]
            bus.publish(project_id, "ideas.sorting", {"sorting": False})

    async def _sort(self, project_id: int, full: bool) -> bool:
        """One call of the model. Returns whether ideas are still waiting for it."""
        async with self.runs._sm() as s:
            project = await s.get(Project, project_id)
            if project is None:
                return False
            ideas = await idea_service.open_ideas(s, project_id)
            pending = [i for i in ideas if i.sort_state == idea_service.PENDING]
            batch = (ideas if full else pending)[: SORT_BATCH if not full else None]
            if not batch:
                return False
            prompt = idea_service.sort_prompt(project, await idea_service.topics(s, project_id), ideas, batch, full)
            provider, model, effort = await self.choice(s, project)
            ids = [i.id for i in batch]
            result, error, manager, account_key = await self._complete(s, project, provider, model, effort, idea_service.SORT_SYSTEM, prompt)
        data = None
        if result is not None:
            try:
                data = idea_service.parse_json(result.text)
            except ValueError:
                error = "The answer held no JSON"
                log.warning("Sorting ideas of project %s: no JSON in %r", project_id, truncate(result.text, 300))
        async with self.runs._sm() as s:
            await self._record(s, project_id, manager, account_key, result, error)
            if data is not None:
                await idea_service.apply_sort(s, project_id, ids, data, full)
            else:
                await idea_service.sort_failed(s, project_id, ids)
            if error:
                bus.publish(project_id, "ideas.sort_failed", {"error": truncate(error, 300)})
                return False  # no retry loop over a broken model: "sort again" retries
            left = (await s.scalars(select(Idea.id).where(Idea.project_id == project_id, Idea.status == IdeaStatus.OPEN, Idea.sort_state == idea_service.PENDING))).first()
            return left is not None

    async def choice(self, s: AsyncSession, project: Project) -> tuple[str, str, str | None]:
        """The project's model for ideas: its own, else the one of its questions."""
        provider, model, effort = await self.runs.questions.choice(s, project)
        if not (project.idea_provider or project.idea_model or project.idea_effort):
            return provider, model, effort
        own = project.idea_provider or provider
        if own != provider and not project.idea_model:
            catalog: list[ProviderInfo] = []
            try:
                catalog = await account_service.catalog(s, await self.runs.runner.providers())
            except Exception:
                catalog = []
            model, effort = questions.default_model(own, catalog), None
        return own, project.idea_model or model, project.idea_effort or effort

    # --- the call ------------------------------------------------------------------------------

    async def _complete(
        self, s: AsyncSession, project: Project, provider: str, model: str, effort: str | None, system: str, prompt: str
    ) -> tuple[CompletionResult | None, str, Agent | None, str | None]:
        """Ask the model; ``s`` is committed first, nothing is held while it answers."""
        manager = await agent_service.get_manager(s, project.id)
        where = self.runs.runners.where(project.id)
        account = await account_service.pick(s, provider, manager, None, model=model, fresh=True, where=where)
        why = ""
        if account is None:
            why = await account_service.shut_out(s, provider, manager) or await account_service.not_signed_in(s, provider, manager, where)
        values = await settings_service.get_all(s)
        credentials = settings_service.credentials(values, account=account_service.fields(account))
        account_key = account.key if account else None
        await s.commit()
        if why:
            return None, why, manager, account_key
        spec = CompletionSpec(provider=provider, model=model, system_prompt=system, prompt=prompt, credentials=credentials, effort=effort)
        try:
            return await self.runs.runners.of(project.id).complete(spec), "", manager, account_key
        except Exception as exc:
            log.warning("Ideas of project %s: the model failed: %s", project.id, exc)
            return None, str(exc) or type(exc).__name__, manager, account_key

    async def _record(self, s: AsyncSession, project_id: int, manager: Agent | None, account_key: str | None, result: CompletionResult | None, error: str) -> None:
        """What it cost, for the statistics: a run of the manager's that holds no text."""
        if manager is None:
            return
        now = utcnow()
        s.add(
            Run(
                project_id=project_id,
                agent_id=manager.id,
                trigger=RunTrigger.IDEAS,
                status=RunStatus.FAILED if error else RunStatus.SUCCEEDED,
                input_text="",
                error=truncate(error, 500) if error else None,
                input_tokens=result.input_tokens if result else 0,
                output_tokens=result.output_tokens if result else 0,
                cost_usd=result.cost_usd if result else 0.0,
                num_turns=1 if result else 0,
                account_key=account_key,
                created_at=now,
                started_at=now,
                finished_at=now,
            )
        )
        await s.commit()

    # --- planning ------------------------------------------------------------------------------

    def plan(self, project_id: int, plan_id: str) -> Plan:
        self._forget_old()
        plan = self._plans.get(plan_id)
        if plan is None or plan.project_id != project_id:
            raise NotFound("This proposal is gone. Plan again.")
        return plan

    def plans(self, project_id: int) -> list[Plan]:
        self._forget_old()
        return [p for p in self._plans.values() if p.project_id == project_id]

    def forget(self, project_id: int, plan_id: str | None) -> None:
        if plan_id and (plan := self._plans.get(plan_id)) is not None and plan.project_id == project_id:
            del self._plans[plan_id]

    def _forget_old(self) -> None:
        now = time.monotonic()
        for key, plan in list(self._plans.items()):
            if plan.status != "running" and now - plan.made > PLAN_KEEP_SECONDS:
                del self._plans[key]

    async def start_plan(self, s: AsyncSession, project: Project, idea_ids: list[int] | None, topic_id: int | None) -> Plan:
        ideas = await idea_service.selection(s, project.id, idea_ids, topic_id)
        mine = sorted(self.plans(project.id), key=lambda p: p.made)
        for old in mine[: max(0, len(mine) - PLANS_PER_PROJECT + 1)]:
            self._plans.pop(old.id, None)
        plan = Plan(id=secrets.token_urlsafe(12), project_id=project.id, idea_ids=[i.id for i in ideas])
        self._plans[plan.id] = plan
        task = asyncio.create_task(self._plan(plan), name=f"ideas-plan-{plan.id}")
        self._planners.add(task)
        task.add_done_callback(self._planners.discard)
        bus.publish(project.id, "idea_plan.updated", plan.out())
        return plan

    async def _plan(self, plan: Plan) -> None:
        try:
            async with self.runs._sm() as s:
                project = await s.get(Project, plan.project_id)
                ideas = list((await s.scalars(select(Idea).where(Idea.id.in_(plan.idea_ids)).order_by(Idea.id))).all())
                prompt = await idea_service.plan_context(s, project, ideas)
                manager = await agent_service.get_manager(s, project.id)
                provider, model, effort = manager.provider, manager.model, manager.effort
                result, error, manager, account_key = await self._complete(s, project, provider, model, effort, idea_service.PLAN_SYSTEM, prompt)
            if result is not None:
                try:
                    plan.tickets, plan.left_out = idea_service.parse_plan(idea_service.parse_json(result.text), project, plan.idea_ids)
                except ValueError as exc:
                    error = f"The proposal could not be read ({exc}). Plan again."
                    log.warning("Planning tickets from ideas of project %s: %r", plan.project_id, truncate(result.text, 300))
            async with self.runs._sm() as s:
                await self._record(s, plan.project_id, manager, account_key, result, error)
            plan.status, plan.error = ("failed", truncate(error, 500)) if error else ("ready", "")
        except Exception as exc:
            log.exception("Planning tickets from ideas of project %s failed", plan.project_id)
            plan.status, plan.error = "failed", truncate(str(exc) or type(exc).__name__, 500)
        finally:
            plan.made = time.monotonic()
            bus.publish(plan.project_id, "idea_plan.updated", plan.out())
