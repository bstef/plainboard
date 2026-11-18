"""Fires due schedules by queueing a run for the schedule's agent."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import Agent, Project, RunTrigger, utcnow
from ..services import board as board_service
from ..services import own_notes as own_note_service
from ..services import schedules as schedule_service
from ..services import todos as todo_service
from . import inbox, prompts
from .run_manager import RunManager

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], runs: RunManager, interval: float):
        self._sm = sessionmaker
        self._runs = runs
        self._interval = interval
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Scheduler tick failed")
            await asyncio.sleep(self._interval)

    async def tick(self) -> int:
        fired = 0
        now = utcnow()
        async with self._sm() as s:
            for schedule in await schedule_service.due(s, now):
                run_id = None
                agent = await s.get(Agent, schedule.agent_id)
                project = await s.get(Project, agent.project_id) if agent is not None else None
                # A paused project: the time passes without a turn, so nothing piles up for the day it goes on.
                if agent is not None and agent.enabled and project is not None and project.paused_at is None:
                    text = f'Scheduled task "{schedule.name}" (schedule {schedule.id}):\n\n{schedule.prompt}'
                    run = await self._runs.enqueue(s, agent, RunTrigger.SCHEDULE, text, schedule_id=schedule.id)
                    run_id = run.id
                    fired += 1
                await schedule_service.mark_fired(s, schedule, run_id, now)
            await board_service.accept_reviews(s)
            await board_service.auto_archive(s)
            await todo_service.purge(s)
            # Arrives with the member's next turn; nobody is woken for it.
            for agent_id, notes in (await own_note_service.take_due_for_review(s)).items():
                await inbox.push(s, agent_id, prompts.own_notes_review([n.id for n in notes], own_note_service.REVIEW_AFTER_DAYS))
        return fired
