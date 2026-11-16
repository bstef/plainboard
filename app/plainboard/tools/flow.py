"""Tools that take waiting out of the agents' hands: handing a pull request to PlainBoard, and
seeing where the team's time went."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..models import Run
from ..orchestration.runner_client import RunnerError
from ..services import projects as project_service
from ..services import pull_requests as pr_service
from ..services import repositories as repo_service
from ..services import settings as settings_service
from ..services import timing as timing_service
from .registry import MANAGER, ToolContext, ToolError, tool


class MergeWhenGreenArgs(BaseModel):
    repository: str = Field(description="Repository name, as in your workspace")
    number: int = Field(ge=1, description="Pull request number")
    merge: bool = Field(default=True, description="false: only watch and report; someone merges by hand")


class TimeReportArgs(BaseModel):
    hours: int = Field(default=24, ge=1, le=336)


@tool(
    "merge_when_green",
    "Hand your pull request to PlainBoard right after opening it: it watches the checks, merges when they pass and gives it back to you if they fail. Then finish; never wait for the checks yourself.",
    MergeWhenGreenArgs,
)
async def merge_when_green(ctx: ToolContext, args: MergeWhenGreenArgs) -> str:
    repos = repo_service.accessible(ctx.agent, await repo_service.list_for_project(ctx.session, ctx.project.id))
    repo = next((r for r in repos if r.name.lower() == args.repository.strip().lower()), None)
    if repo is None:
        raise ToolError(f"No repository '{args.repository}'. Yours: {', '.join(r.name for r in repos) or 'none'}")
    slug = pr_service.github_slug(repo)
    credentials = settings_service.credentials(await settings_service.get_all(ctx.session), project_service.github_token(ctx.project))
    try:
        state = await ctx.runs.runner.pull_request(credentials, slug, args.number)
    except RunnerError as exc:
        raise ToolError(f"Could not read PR #{args.number} of {slug}: {str(exc)[:300]}") from exc
    if state.state != "open":
        return f"PR #{args.number} is already {state.state}: nothing to watch."
    task_id = ctx.run.task_id
    if task_id is None and (run := await ctx.session.get(Run, ctx.run.run_id)):
        task_id = run.task_id
    pr = await pr_service.track(ctx.session, repo, args.number, state, agent_id=ctx.agent.id, task_id=task_id, merge=args.merge)
    timing_service.forget_cache()
    then = "merges it when they pass" if pr.merge else "tells the manager when they pass (it is merged by hand)"
    return (
        f"PR #{pr.number} is with PlainBoard now: it watches the checks, {then}, and gives it back to you as an assignment if they fail. "
        "Your part is done: don't wait for the checks or look at them again. Finish and report."
    )


@tool("time_report", "Where the team's time went: work against waiting, the slowest commands, pull request checks, repeated runs.", TimeReportArgs, roles=(MANAGER,))
async def time_report(ctx: ToolContext, args: TimeReportArgs) -> str:
    overview = await timing_service.overview(ctx.session, ctx.project.id, args.hours)
    return timing_service.report_text(overview, await timing_service.waiting_pull_requests(ctx.session, ctx.project.id))
