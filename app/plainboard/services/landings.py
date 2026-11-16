"""Landings: finished branches that PlainBoard brings to the base branch (the merge_when_green tool).

A check run is the slowest step of most work, and agents start one for every small change if they
are left to it. So they don't start any. An agent pushes its branch, hands it over and is done.
PlainBoard merges all branches that are ready into one train and has it checked once; one train at a
time per repository, so the number of check runs follows the clock and not the number of changes.
The watcher (orchestration/landing.py) does this without any model.

How a train is checked depends on the repository:
- its check command (``repository.check_command``): PlainBoard runs it on the merged branches and
  pushes to the base branch if it succeeds. Plain git, any host.
- no command, but on GitHub with the general token: a pull request for the merged branches, whose
  checks decide.
"""

from __future__ import annotations

import re

from plainboard_protocol import RepoSpec
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Landing, LandingStatus, Repository, Train, TrainStatus
from . import repositories as repo_service

GITHUB_URL = re.compile(r"github\.com[:/]+([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
MAX_FAILED_ROUNDS = 3  # after this many, the author stops getting it back and the manager decides
MAX_TRAIN = 12  # branches on one train
ACTIVE = (LandingStatus.WAITING, LandingStatus.TRAIN, LandingStatus.BACK)
RUNNING_TRAINS = (TrainStatus.CHECKING, TrainStatus.BLOCKED)
COMMAND, GITHUB = "command", "github"


def github_slug(repo: Repository) -> str | None:
    """owner/name if the repository is on GitHub and reached with the general token (what gh uses)."""
    found = GITHUB_URL.search(repo.url.strip())
    return f"{found.group(1)}/{found.group(2)}" if found and repo.auth == "default" else None


def mode(repo: Repository) -> str | None:
    """How this repository's trains are checked; None if PlainBoard cannot land its branches."""
    if repo.check_command.strip():
        return COMMAND
    return GITHUB if github_slug(repo) else None


UNAVAILABLE = (
    "PlainBoard cannot land branches of {name}: it has no check command and is not on GitHub with the general token. "
    "Open a merge request yourself and name it in your report. (The manager can set a check command with update_repository.)"
)


def train_key(repo: Repository) -> str:
    """Names the runner's own clone of the repository."""
    return f"p{repo.project_id}-r{repo.id}"


def remote(repo: Repository) -> RepoSpec:
    (spec,) = repo_service.specs([repo])
    return spec


async def find(session: AsyncSession, repository_id: int, branch: str) -> Landing | None:
    q = select(Landing).where(Landing.repository_id == repository_id, Landing.branch == branch).order_by(Landing.id.desc()).limit(1)
    return await session.scalar(q)


async def hand_over(session: AsyncSession, repo: Repository, branch: str, head_sha: str, *, agent_id: int, task_id: int | None, fresh_start: bool) -> Landing:
    """Put a branch on the list for the next train. ``fresh_start`` (the manager hands it over, or
    it comes with new commits after it had landed) forgets its failed rounds."""
    landing = await find(session, repo.id, branch)
    if landing is None or landing.status == LandingStatus.MERGED:
        landing = Landing(project_id=repo.project_id, repository_id=repo.id, branch=branch)
        session.add(landing)
    elif fresh_start:
        landing.failures, landing.failed_sha = 0, ""
    landing.agent_id, landing.head_sha, landing.title = agent_id, head_sha, branch[:300]
    landing.task_id = task_id or landing.task_id
    # On a train already: its result decides. Being repaired: it goes on when the repair is finished,
    # together with the branches it failed with.
    if landing.status not in (LandingStatus.TRAIN, LandingStatus.BACK):
        landing.status, landing.train_id, landing.group_no = LandingStatus.WAITING, None, None
    await session.commit()
    return landing


async def running_train(session: AsyncSession, repository_id: int) -> Train | None:
    q = select(Train).where(Train.repository_id == repository_id, Train.status.in_(RUNNING_TRAINS)).order_by(Train.id.desc()).limit(1)
    return await session.scalar(q)


async def members(session: AsyncSession, train: Train) -> list[Landing]:
    q = select(Landing).where(Landing.train_id == train.id, Landing.status == LandingStatus.TRAIN).order_by(Landing.id)
    return list((await session.scalars(q)).all())


async def active_for_project(session: AsyncSession, project_id: int) -> list[Landing]:
    q = select(Landing).where(Landing.project_id == project_id, Landing.status.in_(ACTIVE)).order_by(Landing.id)
    return list((await session.scalars(q)).all())


async def next_group(session: AsyncSession, repository_id: int) -> int:
    """A number above every group used so far, for the branches of a train that failed together."""
    used = [
        await session.scalar(select(func.max(Landing.group_no)).where(Landing.repository_id == repository_id)) or 0,
        await session.scalar(select(func.max(Train.group_no)).where(Train.repository_id == repository_id)) or 0,
    ]
    return max(used) + 1
