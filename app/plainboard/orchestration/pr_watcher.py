"""Watches the pull requests agents handed over (merge_when_green) and brings them to the main branch.

Waiting for checks is the slowest step of most work, and the one a model handles worst: it has no
clock, looks again and again (each look re-reads its context), or sits in a turn until they finish.
Here nobody waits. A timer asks GitHub; when every check of a pull request has finished it is merged,
or its author gets all failures at once as one assignment. After a few failed rounds the manager decides.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from plainboard_protocol import PullRequestState
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import Agent, Assignment, AssignmentStatus, Project, PullRequest, PullRequestStatus, Repository, Task, utcnow
from ..services import agents as agent_service
from ..services import board as board_service
from ..services import projects as project_service
from ..services import pull_requests as pr_service
from ..services import settings as settings_service
from ..services import timing as timing_service
from ..services.errors import ServiceError
from . import inbox, prompts
from .run_manager import RunManager
from .runner_client import RunnerError

log = logging.getLogger(__name__)

# Checks appear a moment after a push. A pull request without any after this time has none.
NO_CHECKS_GRACE_SECONDS = 120


def checks_result(state: PullRequestState, round_age: float) -> str:
    """pending | failed | passed for one round of checks. Failures are reported only when every check
    has finished, so the author gets all of them at once instead of one per round."""
    if any(c.state == "pending" for c in state.checks):
        return "pending"
    if any(c.state == "fail" for c in state.checks):
        return "failed"
    if not state.checks and round_age < NO_CHECKS_GRACE_SECONDS:
        return "pending"
    return "passed"


class PullRequestWatcher:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], runs: RunManager, interval: float):
        self._sm = sessionmaker
        self._runs = runs
        self._interval = interval
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="pr-watcher")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Pull request watcher tick failed")
            await asyncio.sleep(self._interval)

    async def tick(self, now: datetime | None = None) -> None:
        async with self._sm() as s:
            for pr in await pr_service.watched(s):
                try:
                    await self._check(s, pr, now or utcnow())
                except (RunnerError, ServiceError) as exc:  # e.g. GitHub or the runner unreachable: next time
                    log.info("Could not check PR #%s: %s", pr.number, exc)

    async def _check(self, s: AsyncSession, pr: PullRequest, now: datetime) -> None:
        repo = await s.get(Repository, pr.repository_id)
        project = await s.get(Project, pr.project_id)
        if repo is None or project is None:
            return
        credentials = settings_service.credentials(await settings_service.get_all(s), project_service.github_token(project))
        slug = pr_service.github_slug(repo)
        state = await self._runs.runner.pull_request(credentials, slug, pr.number)

        if state.head_sha and state.head_sha != pr.head_sha:  # a push: a new round of checks
            pr.head_sha, pr.round_started_at, pr.checks = state.head_sha, now, "pending"
        if state.state == "merged":
            return await self._merged(s, pr, project, now)
        if state.state == "closed":
            pr.status = PullRequestStatus.CLOSED
            await s.commit()
            return await self._tell_manager(s, pr, project, "was closed without being merged. PlainBoard no longer watches it.")
        if pr.handled_sha == pr.head_sha:  # this head's result is with its author (or waits for a merge by hand)
            if pr.checks == "failed" and state.mergeable != "conflict" and any(c.state == "pending" for c in state.checks):
                pr.handled_sha, pr.round_started_at, pr.checks = "", now, "pending"  # its checks were started again without a push
            elif pr.checks == "failed" and not await self._open_fix(s, pr):
                # The author is done with it and nothing changed: nobody would ever look at it again.
                pr.status = PullRequestStatus.STUCK
                await s.commit()
                return await self._tell_manager(s, pr, project, "still fails, and its author finished the repair without a new push. PlainBoard stopped watching it. Read the report on the ticket and decide how to go on; merge_when_green starts the watch again.")
            await s.commit()
            return
        # A conflict keeps GitHub from running the checks at all, so it counts as a failed round right away.
        conflict = state.mergeable == "conflict"
        result = "failed" if conflict else checks_result(state, (now - pr.round_started_at).total_seconds())
        pr.checks = result
        if result == "pending" or (result == "passed" and pr.merge and state.mergeable == "unknown"):
            await s.commit()  # checks still running, or GitHub has not worked out yet whether it can be merged
            return
        if not conflict:
            pr.rounds += 1
            pr.check_seconds += max(int((now - pr.round_started_at).total_seconds()), 0)
            timing_service.forget_cache()
        if result == "failed":
            pr.failures += 1
            pr.handled_sha = pr.head_sha
            await s.commit()
            failed = [(c.name, c.link) for c in state.checks if c.state == "fail"]
            what = "conflicts with its base branch" if conflict else "failed its checks (" + ", ".join(name for name, _link in failed[:8]) + ")"
            if pr.failures >= pr_service.MAX_FAILED_ROUNDS:
                # A bound on the loop of repair and re-check: after this, someone has to think again.
                pr.status = PullRequestStatus.STUCK
                await s.commit()
                return await self._tell_manager(
                    s, pr, project,
                    f"{what}, {pr.failures} rounds in a row now. PlainBoard stopped watching it. Decide how to go on "
                    "(another approach, the failing check itself, the user); merge_when_green starts the watch again.",
                )
            if conflict:
                return await self._back_to_author(s, pr, project, f"Resolve the conflicts of PR #{pr.number}", prompts.fix_conflict_instructions(pr.number, pr.url), what)
            instructions = prompts.fix_checks_instructions(pr.number, pr.url, failed, pr.failures, pr_service.MAX_FAILED_ROUNDS)
            return await self._back_to_author(s, pr, project, f"Fix the failed checks of PR #{pr.number}", instructions, what)
        if not pr.merge:
            pr.handled_sha = pr.head_sha
            await s.commit()
            return await self._tell_manager(s, pr, project, "passed its checks and waits to be merged by hand.")
        merged = await self._runs.runner.merge_pull_request(credentials, slug, pr.number)
        if merged.ok:
            return await self._merged(s, pr, project, now)
        pr.handled_sha, pr.status = pr.head_sha, PullRequestStatus.STUCK
        await s.commit()
        await self._tell_manager(s, pr, project, f"passed its checks but could not be merged: {merged.detail[-300:]} PlainBoard stopped watching it; merge_when_green starts the watch again.")

    async def _ticket(self, s: AsyncSession, pr: PullRequest) -> Task | None:
        return await s.get(Task, pr.task_id) if pr.task_id else None

    async def _label(self, s: AsyncSession, pr: PullRequest) -> str:
        author = await s.get(Agent, pr.agent_id) if pr.agent_id else None
        ticket = await self._ticket(s, pr)
        about = ", ".join(part for part in (author.name if author else "", f"ticket #{ticket.number}" if ticket else "") if part)
        return f'PR #{pr.number} "{pr.title}"' + (f" ({about})" if about else "")

    async def _merged(self, s: AsyncSession, pr: PullRequest, project: Project, now: datetime) -> None:
        pr.status, pr.merged_at, pr.checks = PullRequestStatus.MERGED, now, "passed"
        await s.commit()
        took = f" Its checks took {timing_service.duration(pr.check_seconds)} in {pr.rounds} round{'s' if pr.rounds != 1 else ''}." if pr.rounds else ""
        if ticket := await self._ticket(s, pr):
            await board_service.add_comment(s, ticket, f"PR #{pr.number} is merged.{took}", author_kind="system")
        await self._tell_manager(s, pr, project, f"is merged.{took} If that finishes its ticket, move it to done.", comment=False)

    async def _tell_manager(self, s: AsyncSession, pr: PullRequest, project: Project, what: str, *, comment: bool = True) -> None:
        if comment and (ticket := await self._ticket(s, pr)):
            await board_service.add_comment(s, ticket, f"PR #{pr.number} {what}", author_kind="system")
        manager = await agent_service.get_manager(s, project.id)
        await inbox.push(s, manager.id, f"{await self._label(s, pr)} {what}")
        if project.auto_wake_manager:
            await self._runs.request_wake(s, manager.id)
        await board_service.publish_task_id(s, pr.task_id)

    async def _back_to_author(self, s: AsyncSession, pr: PullRequest, project: Project, title: str, instructions: str, what: str) -> None:
        """Give the pull request back to its author as an assignment, unless one for it is still open."""
        author = await s.get(Agent, pr.agent_id) if pr.agent_id else None
        if author is None or not author.enabled:
            pr.status = PullRequestStatus.STUCK
            await s.commit()
            return await self._tell_manager(s, pr, project, f"{what}, and its author is not available. Give the repair to someone; merge_when_green starts the watch again.")
        ticket = await self._ticket(s, pr)
        if ticket:
            await board_service.add_comment(s, ticket, f"PR #{pr.number} {what}. Back to {author.name}.", author_kind="system")
        if not await self._open_fix(s, pr):
            await self._runs.delegate(s, None, author, title, instructions, ticket, pull_request_id=pr.id)

    @staticmethod
    async def _open_fix(s: AsyncSession, pr: PullRequest) -> bool:
        open_fix = select(Assignment.id).where(Assignment.pull_request_id == pr.id, Assignment.status.in_((AssignmentStatus.QUEUED, AssignmentStatus.RUNNING)))
        return bool(await s.scalar(open_fix.limit(1)))
