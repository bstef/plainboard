"""Tools that take waiting out of the agents' hands: handing a finished branch to PlainBoard, and
seeing where the team's time went."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..models import LandingStatus, Run
from ..orchestration.runner_client import RunnerError
from ..services import landings as landing_service
from ..services import projects as project_service
from ..services import repositories as repo_service
from ..services import settings as settings_service
from ..services import timing as timing_service
from .registry import MANAGER, ToolContext, ToolError, tool


class MergeWhenGreenArgs(BaseModel):
    repository: str = Field(description="Repository name, as in your workspace")
    branch: str = Field(description="Your pushed branch with the finished change")


class TimeReportArgs(BaseModel):
    hours: int = Field(default=24, ge=1, le=336)


@tool(
    "merge_when_green",
    "Hand your pushed, finished branch to PlainBoard: it checks it together with the other finished branches (one check run for all), merges what passes and gives a failing branch back to its author. Then finish: open no pull request, start no checks, wait for none.",
    MergeWhenGreenArgs,
)
async def merge_when_green(ctx: ToolContext, args: MergeWhenGreenArgs) -> str:
    repos = repo_service.accessible(ctx.agent, await repo_service.list_for_project(ctx.session, ctx.project.id))
    repo = next((r for r in repos if r.name.lower() == args.repository.strip().lower()), None)
    if repo is None:
        raise ToolError(f"No repository '{args.repository}'. Yours: {', '.join(r.name for r in repos) or 'none'}")
    if landing_service.mode(repo) is None:
        raise ToolError(landing_service.UNAVAILABLE.format(name=repo.name))
    branch = args.branch.strip().removeprefix("origin/")
    if not branch or branch == (repo.default_branch or ""):
        raise ToolError("Name the branch with your change, not the base branch.")
    credentials = settings_service.credentials(await settings_service.get_all(ctx.session), project_service.github_token(ctx.project))
    try:
        head = await ctx.runs.runner.branch_head(credentials, landing_service.remote(repo), branch)
    except RunnerError as exc:
        raise ToolError(f"Could not reach {repo.name}: {str(exc)[:300]}") from exc
    if head is None:
        raise ToolError(f"Branch {branch} is not on the remote of {repo.name}: push it first, then hand it over.")
    task_id = ctx.run.task_id
    if task_id is None and (run := await ctx.session.get(Run, ctx.run.run_id)):
        task_id = run.task_id
    before = await landing_service.find(ctx.session, repo.id, branch)
    if before and before.status == LandingStatus.STUCK and before.failed_sha == head and not ctx.agent.is_manager:
        raise ToolError(f"Branch {branch} failed with exactly this commit and went to the manager. Push a change first, or leave it to the manager.")
    landing = await landing_service.hand_over(ctx.session, repo, branch, head, agent_id=ctx.agent.id, task_id=task_id, fresh_start=ctx.agent.is_manager)
    timing_service.forget_cache()
    if landing.status == LandingStatus.TRAIN:
        return f"Branch {branch} is being checked already. Commits pushed since then are not part of that check: hand the branch over again when it has landed."
    return (
        f"Branch {branch} is with PlainBoard now: it is checked together with the other finished branches (one check run for all of them) and merged into "
        f"{repo.default_branch or 'the base branch'}. If it is the one that fails, it comes back to you as an assignment. "
        "Your part is done: open no pull request, start no checks and wait for none. Finish and report."
    )


@tool("time_report", "Where the team's time went: work against waiting, the slowest commands, check runs for landing, repeated runs.", TimeReportArgs, roles=(MANAGER,))
async def time_report(ctx: ToolContext, args: TimeReportArgs) -> str:
    overview = await timing_service.overview(ctx.session, ctx.project.id, args.hours)
    return timing_service.report_text(overview, await timing_service.running_trains(ctx.session, ctx.project.id))
