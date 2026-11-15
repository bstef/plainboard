"""Background jobs: slow commands that keep running after the agent's turn has ended."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..models import JobStatus, utcnow
from ..services import agents as agent_service
from ..services import jobs as job_service
from ..services.util import truncate
from .registry import ToolContext, tool


class StartJobArgs(BaseModel):
    command: str = Field(min_length=1, description="Shell command; runs in your workspace")


class StopJobArgs(BaseModel):
    id: int


@tool(
    "start_job",
    "Run a slow shell command (build, install, test suite) outside your turn, so you stay available. Then finish your turn, without waiting for it (no sleep, tail or Monitor): the result arrives as an update when it ends.",
    StartJobArgs,
)
async def start_job(ctx: ToolContext, args: StartJobArgs) -> str:
    job = await ctx.runs.start_job(ctx.session, ctx.project, ctx.agent, args.command)
    return f"Job #{job.id} started. Log: {job.log_path}. Don't wait for it: end your turn when nothing else is left, its result wakes you."


def _age(job) -> str:
    hours = (utcnow() - job.created_at).total_seconds() / 3600
    return f"{hours / 24:.0f} d" if hours >= 48 else f"{hours:.0f} h" if hours >= 1 else "<1 h"


@tool("list_jobs", "Your running and recent background jobs. Manager: also the team's running ones.")
async def list_jobs(ctx: ToolContext, _args) -> str:
    jobs = await job_service.recent_for_agent(ctx.session, ctx.agent.id)
    lines = []
    for job in jobs:
        code = f", exit {job.exit_code}" if job.exit_code is not None else ""
        lines.append(f"#{job.id} {job.status}{code}: {truncate(job.command, 100)} · log: {job.log_path}")
    if ctx.agent.is_manager:
        names = {a.id: a.name for a in await agent_service.list_for_project(ctx.session, ctx.project.id)}
        team = [j for j in await job_service.running_for_project(ctx.session, ctx.project.id) if j.agent_id != ctx.agent.id]
        if team:
            lines.append("Running in the team:")
            lines += [f"#{j.id} {names.get(j.agent_id, '?')}, since {_age(j)}: {truncate(j.command, 100)}" for j in team]
    return "\n".join(lines) or "No jobs."


@tool("stop_job", "Stop one of your running background jobs. Manager: any job of the team.", StopJobArgs)
async def stop_job(ctx: ToolContext, args: StopJobArgs) -> str:
    if ctx.agent.is_manager:
        job = await job_service.get_for_project(ctx.session, ctx.project.id, args.id)
    else:
        job = await job_service.get_for_agent(ctx.session, ctx.agent.id, args.id)
    if job.status != JobStatus.RUNNING or not await ctx.runs.stop_job(ctx.session, job):
        return f"Job #{job.id} has already ended."
    return f"Job #{job.id} stopped."
