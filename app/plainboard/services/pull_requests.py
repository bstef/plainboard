"""Pull requests that PlainBoard watches for the team (the merge_when_green tool).

Waiting for checks is not work for an agent: every look costs a turn that re-reads its context, and
a turn that waits keeps the agent from its next work. So an agent hands its pull request over and is
done; the watcher (orchestration/pr_watcher.py) does the waiting and the merge without any model."""

from __future__ import annotations

import re

from plainboard_protocol import PullRequestState
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import PullRequest, PullRequestStatus, Repository, utcnow
from .errors import ServiceError

GITHUB_URL = re.compile(r"github\.com[:/]+([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
MAX_FAILED_ROUNDS = 3  # after this many, the author stops getting it back and the manager decides


def github_slug(repo: Repository) -> str:
    """owner/name of a repository PlainBoard can watch: on GitHub, reached with the general token."""
    found = GITHUB_URL.search(repo.url.strip())
    if not found:
        raise ServiceError(f"{repo.name} is not on GitHub, so PlainBoard cannot watch its pull requests. Watch the checks yourself with a job.")
    if repo.auth != "default":
        raise ServiceError(f"{repo.name} uses its own access, which the GitHub CLI cannot use. Watch the checks yourself with a job.")
    return f"{found.group(1)}/{found.group(2)}"


async def get(session: AsyncSession, repository_id: int, number: int) -> PullRequest | None:
    return await session.scalar(select(PullRequest).where(PullRequest.repository_id == repository_id, PullRequest.number == number))


async def track(
    session: AsyncSession, repo: Repository, number: int, state: PullRequestState, *, agent_id: int, task_id: int | None, merge: bool
) -> PullRequest:
    """Start (or restart) watching a pull request. A new hand-over gives it its failed rounds back."""
    pr = await get(session, repo.id, number)
    if pr is None:
        pr = PullRequest(project_id=repo.project_id, repository_id=repo.id, number=number)
        session.add(pr)
    pr.url, pr.title, pr.agent_id, pr.merge = state.url, state.title[:300], agent_id, merge
    pr.task_id = task_id or pr.task_id
    pr.status, pr.checks, pr.failures = PullRequestStatus.OPEN, "pending", 0
    pr.head_sha, pr.handled_sha, pr.round_started_at = state.head_sha, "", utcnow()
    await session.commit()
    return pr


async def watched(session: AsyncSession) -> list[PullRequest]:
    return list((await session.scalars(select(PullRequest).where(PullRequest.status == PullRequestStatus.OPEN).order_by(PullRequest.id))).all())


async def open_for_project(session: AsyncSession, project_id: int) -> list[PullRequest]:
    q = select(PullRequest).where(PullRequest.project_id == project_id, PullRequest.status == PullRequestStatus.OPEN).order_by(PullRequest.id)
    return list((await session.scalars(q)).all())
