"""Pause everything: all projects come to rest, e.g. before PlainBoard is updated or its server restarts.

PlainBoard does it itself, no manager is asked and no turn is spent on it: every member is paused as
``pause_team`` does (its open assignment stays and goes on afterwards), the turns that run are told to
wrap up, and nothing new starts: no turn, no train (``RunManager.pause_all``). Background jobs are
stopped by PlainBoard itself as soon as their agent has no turn any more, servers of previews
included: the restart would end them anyway, and their agent reads with its next turn which ones to
start again. What still moves is counted from the database, so "everything is at rest" is known, not
guessed. The pause is a setting: it survives the restart it was made for, until someone resumes.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Agent, Job, JobStatus, Project, Run, RunStatus, Train, utcnow
from ..schemas import PauseMoving, PauseOut
from . import agents as agent_service
from . import landings as landing_service
from . import settings as settings_service
from .util import truncate

KEY = "pause"


async def state(session: AsyncSession) -> dict | None:
    """Who paused everything, when, and which members that paused. None: not paused."""
    return await settings_service.get(session, KEY)


async def begin(session: AsyncSession, by: str) -> list[Agent]:
    """Pause the members of every project and remember which ones, so the end enables exactly those
    again and leaves alone whoever was off before. Returns them."""
    paused: list[Agent] = []
    for project_id in await session.scalars(select(Project.id).order_by(Project.id)):
        paused += await agent_service.pause_team(session, project_id)
    earlier = await state(session) or {}
    record = {"by": earlier.get("by") or by, "at": earlier.get("at") or utcnow().isoformat(), "agents": sorted({*earlier.get("agents", []), *(a.id for a in paused)})}
    await settings_service.update(session, {KEY: record}, managed=True)
    for agent in paused:
        await agent_service.publish(session, agent)
    return paused


async def end(session: AsyncSession) -> list[Agent]:
    """Enable the members the pause switched off (not one that was switched by hand meanwhile). Returns them."""
    record = await state(session) or {}
    ids = record.get("agents", [])
    members = list(await session.scalars(select(Agent).where(Agent.id.in_(ids), Agent.team_pause).order_by(Agent.id))) if ids else []
    for agent in members:
        agent.enabled, agent.team_pause = True, False
    await session.commit()
    await settings_service.update(session, {KEY: None}, managed=True)
    for agent in members:
        await agent_service.publish(session, agent)
    return members


async def status(session: AsyncSession) -> PauseOut:
    """Whether everything is paused, and what still moves: turns that run or are about to wrap up, jobs
    an assignment waits for, and checks of finished branches that run in the runner."""
    record = await state(session)
    if record is None:
        return PauseOut(paused=False)
    projects = dict((await session.execute(select(Project.id, Project.name))).all())
    agents = dict((await session.execute(select(Agent.id, Agent.name))).all())
    moving: list[PauseMoving] = []
    turns = select(Run).where((Run.status == RunStatus.RUNNING) | ((Run.status == RunStatus.QUEUED) & Run.stop_requested_at.is_not(None))).order_by(Run.id)
    for run in await session.scalars(turns):
        moving.append(PauseMoving(what="turn", project=projects.get(run.project_id, ""), agent=agents.get(run.agent_id, "")))
    jobs = list(await session.scalars(select(Job).where(Job.status == JobStatus.RUNNING).order_by(Job.id)))
    for job in jobs:
        if job.assignment_id is not None:
            moving.append(PauseMoving(what="job", project=projects.get(job.project_id, ""), agent=agents.get(job.agent_id, ""), detail=truncate(job.command, 80)))
    checks = select(Train).where(Train.status.in_(landing_service.RUNNING_TRAINS), Train.job_id.is_not(None)).order_by(Train.id)
    for train in await session.scalars(checks):
        moving.append(PauseMoving(what="check", project=projects.get(train.project_id, ""), detail=train.branch))
    return PauseOut(
        paused=True,
        by=record.get("by", ""),
        since=datetime.fromisoformat(record["at"]) if record.get("at") else None,
        standing=not moving,
        moving=moving,
        servers=sum(job.assignment_id is None for job in jobs),
    )
