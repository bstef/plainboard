"""Tools that take waiting out of the agents' hands: handing a finished branch to PlainBoard, and
seeing where the team's time went."""

from __future__ import annotations

import re
import shlex

from pydantic import BaseModel, Field

from ..models import Assignment, JobStatus, LandingStatus, Repository, Run
from ..orchestration import prompts
from ..orchestration.runner_client import RunnerError
from ..services import landings as landing_service
from ..services import projects as project_service
from ..services import repositories as repo_service
from ..services import settings as settings_service
from ..services import timing as timing_service
from ..services.errors import WaitEnded
from .registry import MANAGER, ToolContext, ToolError, tool

QUICK_CHECK_SECONDS = 600  # a quick check that takes longer is not one: it is stopped and the branch goes on
NOT_AS_PUSHED = 97  # exit code: the clone is not on the commit that is handed over


class MergeWhenGreenArgs(BaseModel):
    repository: str = Field(description="Repository name, as in your workspace")
    branch: str = Field(description="Your pushed branch with the finished change")


class ReturnBranchArgs(BaseModel):
    repository: str
    branch: str
    reason: str = Field(min_length=10, description="What fails and why it is more than a small fix")


class TimeReportArgs(BaseModel):
    hours: int = Field(default=24, ge=1, le=336)


async def _repository(ctx: ToolContext, name: str) -> Repository:
    repos = repo_service.accessible(ctx.agent, await repo_service.list_for_project(ctx.session, ctx.project.id))
    repo = next((r for r in repos if r.name.lower() == name.strip().lower()), None)
    if repo is None:
        raise ToolError(f"No repository '{name}'. Yours: {', '.join(r.name for r in repos) or 'none'}")
    return repo


async def _quick_check(ctx: ToolContext, repo: Repository, branch: str, head: str) -> str:
    """Run the repository's quick check on what is handed over, in the agent's own clone (its build
    cache is warm there). A failure comes back in this call, while the agent still knows the change;
    it never reaches a train, where it would cost everybody a check run."""
    if not re.fullmatch(r"[0-9a-f]{7,64}", head):
        return ""
    guard = f'cd {shlex.quote(repo.name)} && git diff --quiet HEAD && [ "$(git rev-parse HEAD)" = "{head}" ] || exit {NOT_AS_PUSHED}'
    job = await ctx.runs.start_job(ctx.session, ctx.project, ctx.agent, f"{guard}\n{repo.quick_check_command.strip()}", ctx.run.assignment_id)
    await ctx.session.commit()  # don't hold a DB transaction while waiting
    try:
        result = await ctx.runs.wait_job(job.id, QUICK_CHECK_SECONDS, ctx.run.run_id)
    except WaitEnded as exc:
        await ctx.runs.stop_job(ctx.session, job, settle=False)
        raise ToolError(f"Not handed over: the quick check was stopped ({exc}). Hand the branch over again afterwards.") from exc
    await ctx.session.refresh(job)
    if job.status == JobStatus.RUNNING:
        await ctx.runs.stop_job(ctx.session, job, settle=False)
        return f" The quick check of {repo.name} did not end within {QUICK_CHECK_SECONDS // 60} min and was skipped: say so in your report, it is not quick."
    if job.exit_code == NOT_AS_PUSHED:
        raise ToolError(
            f"Not handed over: your clone of {repo.name} is not on the commit you pushed ({head[:10]}), or has uncommitted changes. "
            f"The quick check runs on what you hand over: check out {branch} as pushed, with a clean tree, and hand it over again."
        )
    if job.exit_code != 0:
        raise ToolError(
            f"Not handed over: the quick check of {repo.name} failed. Fix all of it, push, and hand the branch over again.\n"
            + (result or f"Job #{job.id} ended with exit code {job.exit_code}. Log: {job.log_path}")
        )
    return " The quick check passed."


@tool(
    "merge_when_green",
    "Hand your pushed, finished branch to PlainBoard: it runs the repository's quick check at once, then checks the branch together with the other finished branches (one check run for all) and merges what passes. Then finish: open no pull request, start no checks, wait for none.",
    MergeWhenGreenArgs,
)
async def merge_when_green(ctx: ToolContext, args: MergeWhenGreenArgs) -> str:
    repo = await _repository(ctx, args.repository)
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
    # The manager hands over what others wrote, from a clone that is elsewhere: its call is a decision, not a check.
    quick = await _quick_check(ctx, repo, branch, head) if repo.quick_check_command.strip() and not ctx.agent.is_manager else ""
    landing = await landing_service.hand_over(ctx.session, repo, branch, head, agent_id=ctx.agent.id, task_id=task_id, fresh_start=ctx.agent.is_manager)
    timing_service.forget_cache()
    if landing.status == LandingStatus.TRAIN:
        return f"Branch {branch} is being checked already. Commits pushed since then are not part of that check: hand the branch over again when it has landed."
    if landing.status == LandingStatus.BACK:
        return f"Branch {branch} is taken as pushed.{quick} It goes on the next train when your repair is finished: finish and report."
    return (
        f"Branch {branch} is with PlainBoard now.{quick} It is checked together with the other finished branches (one check run for all of them) and merged into "
        f"{repo.default_branch or 'the base branch'}. If that run fails, PlainBoard has it repaired; you may get it back as an assignment. "
        "Your part is done: open no pull request, start no checks and wait for none. Finish and report."
    )


@tool(
    "return_branch",
    "Only while you repair branches that failed together: give one that needs more than a small fix back to its author, so the others land without it.",
    ReturnBranchArgs,
)
async def return_branch(ctx: ToolContext, args: ReturnBranchArgs) -> str:
    from ..orchestration import landing as landing_flow  # it imports the run manager, which loads the tools

    repo = await _repository(ctx, args.repository)
    branch = args.branch.strip().removeprefix("origin/")
    landing = await landing_service.find(ctx.session, repo.id, branch)
    assignment = await ctx.session.get(Assignment, ctx.run.assignment_id) if ctx.run.assignment_id else None
    repairing = bool(assignment and assignment.train_id and landing and landing.status == LandingStatus.BACK and landing.train_id == assignment.train_id)
    if not repairing and not (ctx.agent.is_manager and landing and landing.status in (LandingStatus.BACK, LandingStatus.WAITING)):
        raise ToolError(f"{branch} is not one of the branches you are repairing together.")
    assert landing
    instructions = prompts.returned_instructions(branch, ctx.agent.name, args.reason, landing.failures, landing_service.MAX_FAILED_ROUNDS)
    went_back = await landing_flow.give_back(
        ctx.session, ctx.runs, repo, ctx.project, landing, f"was taken off a failed train by {ctx.agent.name}", f"Repair branch {branch}", instructions, counts=False
    )
    timing_service.forget_cache()
    where = "is back with its author" if went_back else "went to the manager"
    return f"Branch {branch} {where}. The others go on without it: none of them may contain it or need it."


@tool("time_report", "Where the team's time went: work against waiting, the slowest commands, check runs for landing, repeated runs.", TimeReportArgs, roles=(MANAGER,))
async def time_report(ctx: ToolContext, args: TimeReportArgs) -> str:
    overview = await timing_service.overview(ctx.session, ctx.project.id, args.hours)
    return timing_service.report_text(overview, await timing_service.running_trains(ctx.session, ctx.project.id))
