"""Brings finished branches to the base branch: together, with one check run for all of them.

A check run (CI, a test suite) is the slowest step of most work. Agents start one for every small
change if they are left to it, and then wait for it. Here they start none and wait for none: an
agent pushes its branch and hands it over (merge_when_green). On a timer, without any model:

- All branches of a repository that are ready are merged into one train, and the train is checked
  once: by the repository's check command, or on GitHub by the checks of a pull request for it.
- One train per repository at a time. What gets ready meanwhile goes on the next one, so the number
  of check runs follows the clock, not the number of changes. A train waits a moment for further
  branches before it leaves.
- A train that passes lands: all its branches are on the base branch.
- A train that fails is cut in halves, each checked on its own, until the branch that breaks the
  checks is found. Only that one goes back to its author, as one assignment. If both halves of a
  train fail, the cause is rarely a branch (a broken base branch, a flaky check): landing stops for
  those branches and the manager is told, instead of more check runs.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime

from plainboard_protocol import Credentials, PullRequestState, RepoSpec, TrainCheckSpec, TrainLandSpec, TrainSpec
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import Agent, Assignment, AssignmentStatus, Landing, LandingStatus, Project, Repository, Task, Train, TrainStatus, utcnow
from ..services import agents as agent_service
from ..services import board as board_service
from ..services import landings as landing_service
from ..services import projects as project_service
from ..services import secrets as secret_service
from ..services import settings as settings_service
from ..services import timing as timing_service
from ..services.errors import ServiceError
from ..services.util import truncate
from . import inbox, prompts
from .run_manager import RunManager
from .runner_client import RunnerError

log = logging.getLogger(__name__)

# Checks appear a moment after a push. A pull request without any after this time has none.
NO_CHECKS_GRACE_SECONDS = 120
MERGE_METHODS = ["merge", "rebase", "squash"]  # a merge commit keeps each branch's commits as they were checked


def checks_result(state: PullRequestState, age: float) -> str:
    """pending | failed | passed. Only when every check has finished: a failure is reported once, with all of them."""
    if any(c.state == "pending" for c in state.checks):
        return "pending"
    if any(c.state == "fail" for c in state.checks):
        return "failed"
    if not state.checks and age < NO_CHECKS_GRACE_SECONDS:
        return "pending"
    return "passed"


def base_moved(detail: str) -> bool:
    """Whether a push was refused only because someone else pushed to the base branch meanwhile."""
    return "fetch first" in detail or "non-fast-forward" in detail


@dataclass
class Repo:
    """A repository with what the watcher needs to reach it."""

    repo: Repository
    project: Project
    credentials: Credentials
    mode: str | None
    slug: str | None
    key: str
    remote: RepoSpec
    redact: object


class LandingWatcher:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], runs: RunManager, interval: float, linger: float = 0.0):
        self._sm = sessionmaker
        self._runs = runs
        self._interval = interval
        self.linger = linger  # seconds a train waits for further branches before it leaves
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="landing-watcher")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Landing watcher tick failed")
            await asyncio.sleep(self._interval)

    async def tick(self, now: datetime | None = None) -> None:
        async with self._sm() as s:
            waiting = await s.scalars(select(Landing.repository_id).where(Landing.status.in_(landing_service.ACTIVE)))
            running = await s.scalars(select(Train.repository_id).where(Train.status.in_(landing_service.RUNNING_TRAINS)))
            for repository_id in sorted({*waiting, *running}):
                try:
                    await self._repository(s, repository_id, now or utcnow())
                except (RunnerError, ServiceError) as exc:  # e.g. the runner or the git host is unreachable: next time
                    await s.rollback()
                    log.info("Landing for repository %s postponed: %s", repository_id, exc)

    async def _repository(self, s: AsyncSession, repository_id: int, now: datetime) -> None:
        repo = await s.get(Repository, repository_id)
        project = await s.get(Project, repo.project_id) if repo else None
        if repo is None or project is None:
            return
        env = secret_service.env(await secret_service.effective(s, project.id))
        r = Repo(
            repo=repo,
            project=project,
            credentials=settings_service.credentials(await settings_service.get_all(s), project_service.github_token(project), env),
            mode=landing_service.mode(repo),
            slug=landing_service.github_slug(repo),
            key=landing_service.train_key(repo),
            remote=landing_service.remote(repo),
            redact=secret_service.redactor(env),
        )
        train = await landing_service.running_train(s, repo.id)
        if train is None or await self._watch(s, r, train, now):
            await self._depart(s, r, now)

    # ------------------------------------------------------------------ a train on its way

    async def _watch(self, s: AsyncSession, r: Repo, train: Train, now: datetime) -> bool:
        """Look at the running train. True once it is over, so the next one can leave."""
        members = await landing_service.members(s, train)
        if not members:
            await self._abort(s, r, train, [], now)
            return True
        if train.mode == landing_service.GITHUB:
            return await self._watch_pull_request(s, r, train, members, now)
        state = (await self._runs.runner.job_status([train.job_id or ""])).get(train.job_id or "")
        if state is None:  # the runner restarted and the check with it: again, with the next train
            await self._abort(s, r, train, members, now)
            return True
        if state.running:
            return False
        self._finished(train, now)
        if state.exit_code != 0:
            evidence = f"Check command: {r.repo.check_command.strip()}\nIt ended with exit code {state.exit_code}. Last output:\n{r.redact(state.tail)}\nFull log: {state.log_path}"  # type: ignore[operator]
            await self._failed(s, r, train, members, f"the check command ended with exit code {state.exit_code}", evidence)
            return True
        spec = TrainLandSpec(credentials=r.credentials, key=r.key, remote=r.remote, base=train.base, head_sha=train.head_sha, delete_branches={m.branch: m.head_sha for m in members})
        landed = await self._runs.runner.land_train(spec)
        if landed.ok:
            await self._landed(s, r, train, members, now)
        elif base_moved(landed.detail):
            await self._abort(s, r, train, members, now)  # assembled and checked again on the new base
        else:
            await self._stuck(s, r, train, members, f"passed the check but could not be pushed to {train.base}: {truncate(landed.detail, 300)}")
        return True

    async def _watch_pull_request(self, s: AsyncSession, r: Repo, train: Train, members: list[Landing], now: datetime) -> bool:
        assert r.slug and train.number
        state = await self._runs.runner.pull_request(r.credentials, r.slug, train.number)
        if state.state == "merged":  # by PlainBoard, or by someone who merged it by hand
            await self._landed(s, r, train, members, now)
            return True
        if state.state == "closed":
            self._finished(train, now)
            await self._stuck(s, r, train, members, f"was on a train whose pull request #{train.number} was closed without a merge")
            return True
        if train.status == TrainStatus.BLOCKED:
            return False  # until someone merges it or closes it
        if state.mergeable == "conflict":  # the base branch moved under it: assemble again
            await self._runs.runner.close_pull_request(r.credentials, r.slug, train.number)
            await self._abort(s, r, train, members, now)
            return True
        result = checks_result(state, (now - train.created_at).total_seconds())
        if result == "pending" or (result == "passed" and state.mergeable == "unknown"):
            return False  # checks still running, or GitHub has not worked out yet whether it can be merged
        self._finished(train, now)
        if result == "failed":
            await self._runs.runner.close_pull_request(r.credentials, r.slug, train.number)
            failed = [c for c in state.checks if c.state == "fail"]
            listed = "\n".join(f"- {c.name}" + (f": {c.link}" if c.link else "") for c in failed[:15])
            evidence = f"Failed checks ({train.url}):\n{listed}\nRead the failed logs once: the links, or gh run view <run id> --log-failed."
            await self._failed(s, r, train, members, "failed checks: " + ", ".join(c.name for c in failed[:8]), evidence)
            return True
        merged = await self._runs.runner.merge_pull_request(
            r.credentials, r.slug, train.number, methods=MERGE_METHODS, key=r.key, remote=r.remote, delete_branches={m.branch: m.head_sha for m in members}
        )
        if merged.ok:
            await self._landed(s, r, train, members, now)
            return True
        train.status = TrainStatus.BLOCKED
        await s.commit()
        branches = ", ".join(m.branch for m in members)
        await self._tell_manager(
            s, r,
            f"The train with {branches} passed its checks but could not be merged: {truncate(merged.detail, 300)} "
            f"Merge pull request #{train.number} by hand or remove what blocks it; PlainBoard notices when it is merged. No further train leaves in {r.repo.name} until then.",
        )
        return False

    @staticmethod
    def _finished(train: Train, now: datetime) -> None:
        train.finished_at = now
        train.check_seconds = max(int((now - train.created_at).total_seconds()), 0)
        timing_service.forget_cache()

    async def _abort(self, s: AsyncSession, r: Repo, train: Train, members: list[Landing], now: datetime) -> None:
        """Give a train up without a result: its branches go on the next one."""
        train.status, train.finished_at = TrainStatus.ABORTED, train.finished_at or now
        for member in members:
            member.status, member.train_id = LandingStatus.WAITING, None
        await s.commit()

    async def _landed(self, s: AsyncSession, r: Repo, train: Train, members: list[Landing], now: datetime) -> None:
        if train.finished_at is None:
            self._finished(train, now)
        train.status = TrainStatus.LANDED
        for member in members:
            member.status, member.merged_at, member.group_no = LandingStatus.MERGED, now, None
        await s.commit()
        one_run = f"one check run of {timing_service.duration(train.check_seconds)} for {len(members)} branch{'es' if len(members) != 1 else ''}"
        for member in members:
            if ticket := await self._ticket(s, member):
                await board_service.add_comment(s, ticket, f"Branch {member.branch} is on {train.base} ({one_run}).", author_kind="system")
        listing = "; ".join([await self._label(s, m) for m in members])
        await self._tell_manager(s, r, f"Landed on {train.base} in {r.repo.name}: {listing}. It took {one_run}. Move the tickets this finishes to done.")

    async def _stuck(self, s: AsyncSession, r: Repo, train: Train, members: list[Landing], what: str) -> None:
        train.status = TrainStatus.FAILED
        for member in members:
            member.status, member.train_id, member.group_no = LandingStatus.STUCK, None, None
        await s.commit()
        branches = "; ".join([await self._label(s, m) for m in members])
        await self._tell_manager(s, r, f"Not landed in {r.repo.name}: {branches} {what}. PlainBoard has stopped for these branches; fix the cause, then hand them over again with merge_when_green.")

    async def _failed(self, s: AsyncSession, r: Repo, train: Train, members: list[Landing], summary: str, evidence: str) -> None:
        """The train did not pass. One branch on it: that is the one. Several: check the halves."""
        train.status = TrainStatus.FAILED
        await s.commit()
        if len(members) == 1:
            instructions = prompts.repair_instructions(members[0].branch, evidence, members[0].failures + 1, landing_service.MAX_FAILED_ROUNDS)
            await self._back(s, r, members[0], f"did not pass on its own ({summary})", f"Repair branch {members[0].branch}", instructions)
            return
        other_half = select(Train.id).where(Train.repository_id == r.repo.id, Train.group_no == (train.group_no or 0) ^ 1, Train.status == TrainStatus.FAILED)
        if train.group_no is not None and await s.scalar(other_half.limit(1)):
            # Both halves of a failed train failed: more check runs would not find one guilty branch.
            rest = list(await s.scalars(select(Landing).where(Landing.repository_id == r.repo.id, Landing.status == LandingStatus.WAITING, Landing.group_no.is_not(None))))
            paused = [*members, *rest]
            for landing in paused:
                landing.status, landing.train_id, landing.group_no = LandingStatus.STUCK, None, None
            await s.commit()
            branches = "; ".join([await self._label(s, m) for m in paused])
            await self._tell_manager(
                s, r,
                f"Landing in {r.repo.name} has stopped: a train failed ({summary}), and so did both of its halves. That points to {train.base} itself or a "
                f"check that fails at random, not to one branch. Not landed: {branches}. Getting that cause fixed is the most valuable work now; "
                "then hand the branches over again with merge_when_green.",
            )
            return
        group = await landing_service.next_group(s, r.repo.id)
        half = (len(members) + 1) // 2
        for index, member in enumerate(members):
            member.status, member.train_id, member.group_no = LandingStatus.WAITING, None, group + (index >= half)
        await s.commit()

    async def _back(self, s: AsyncSession, r: Repo, landing: Landing, what: str, title: str, instructions: str) -> None:
        """Give a branch back to its author as one assignment; after a few rounds the manager decides."""
        landing.failures += 1
        landing.failed_sha, landing.train_id, landing.group_no = landing.head_sha, None, None
        author = await s.get(Agent, landing.agent_id) if landing.agent_id else None
        ticket = await self._ticket(s, landing)
        if landing.failures >= landing_service.MAX_FAILED_ROUNDS or author is None or not author.enabled:
            landing.status = LandingStatus.STUCK
            await s.commit()
            why = f"{landing.failures} times now" if author and author.enabled else "and its author is not available"
            await self._tell_manager(
                s, r,
                f"Not landed in {r.repo.name}: {await self._label(s, landing)} {what}, {why}. PlainBoard has stopped for this branch. "
                "Decide how to go on (another approach, the failing check itself, the user); merge_when_green puts it on a train again.",
            )
            return
        landing.status = LandingStatus.BACK
        await s.commit()
        if ticket:
            await board_service.add_comment(s, ticket, f"Branch {landing.branch} {what}. Back to {author.name}.", author_kind="system")
        if not await self._open_repair(s, landing):
            await self._runs.delegate(s, None, author, title, instructions, ticket, landing_id=landing.id)

    # ------------------------------------------------------------------ the next train

    async def _depart(self, s: AsyncSession, r: Repo, now: datetime) -> None:
        """Assemble the next train from what is ready, and start its check."""
        ready = select(Landing).where(Landing.repository_id == r.repo.id)
        for landing in list(await s.scalars(ready.where(Landing.status == LandingStatus.BACK))):
            if not await self._open_repair(s, landing):  # repaired: it goes on the next train by itself
                landing.status = LandingStatus.WAITING
        await s.commit()
        waiting = list(await s.scalars(ready.where(Landing.status == LandingStatus.WAITING).order_by(Landing.id)))
        for landing in [w for w in waiting if not w.branch]:  # handed over as a pull request number, before trains
            state = await self._runs.runner.pull_request(r.credentials, r.slug, landing.number) if r.slug and landing.number else None
            landing.branch = state.branch if state and state.state == "open" else ""
            if not landing.branch:
                landing.status = LandingStatus.CLOSED
        waiting = [w for w in waiting if w.branch]
        # While a failed train is being narrowed down, its halves go first, one at a time.
        groups = {w.group_no for w in waiting if w.group_no is not None}
        group = min(groups) if groups else None
        pick = [w for w in waiting if w.group_no == group][: landing_service.MAX_TRAIN]
        await s.commit()
        if not pick:
            return
        if group is None and self.linger > 0 and (now - min(p.updated_at for p in pick)).total_seconds() < self.linger:
            return  # more branches may be about to get ready: a fuller train means fewer check runs
        if r.mode is None:
            for landing in pick:
                landing.status = LandingStatus.STUCK
            await s.commit()
            await self._tell_manager(s, r, f"Not landed: {', '.join(p.branch for p in pick)}. " + landing_service.UNAVAILABLE.format(name=r.repo.name))
            return

        train = Train(project_id=r.project.id, repository_id=r.repo.id, mode=r.mode, group_no=group, status=TrainStatus.CHECKING, created_at=now)
        s.add(train)
        await s.flush()
        tickets = {p.id: await self._ticket(s, p) for p in pick}
        body = "Checked together by PlainBoard, so the checks run once for all of these branches:\n\n" + "\n".join(
            f"- {p.branch}" + (f" (ticket #{tickets[p.id].number}: {tickets[p.id].title})" if tickets[p.id] else "") for p in pick
        )
        spec = TrainSpec(
            credentials=r.credentials,
            key=r.key,
            remote=r.remote,
            base=r.repo.default_branch or None,
            branch=f"plainboard/train-{train.id}",
            members=[p.branch for p in pick],
            skip_unchanged={p.branch: p.failed_sha for p in pick if p.failed_sha},
            github=r.slug if r.mode == landing_service.GITHUB else None,
            title="Land " + truncate(", ".join(p.branch for p in pick), 200),
            body=body,
        )
        result = await self._runs.runner.assemble_train(spec)
        by_branch = {p.branch: p for p in pick}
        on_train = [by_branch[b] for b in result.merged if b in by_branch]
        for landing in on_train:
            landing.status, landing.train_id, landing.head_sha = LandingStatus.TRAIN, train.id, result.merged[landing.branch]
        train.branch, train.base, train.head_sha, train.size = spec.branch, result.base, result.head_sha, len(on_train)
        train.number, train.url = result.number, result.url
        if on_train and r.mode == landing_service.COMMAND:
            train.job_id = secrets.token_hex(8)
            await self._runs.runner.check_train(TrainCheckSpec(credentials=r.credentials, key=r.key, id=train.job_id, command=r.repo.check_command.strip()))
        if not on_train:
            await s.delete(train)
        await s.commit()

        # What could not go on the train.
        for branch in result.landed:
            landing = by_branch[branch]
            landing.status, landing.merged_at, landing.group_no = LandingStatus.MERGED, now, None
            await s.commit()
            if ticket := tickets[landing.id]:
                await board_service.add_comment(s, ticket, f"Branch {branch} is on {result.base} already.", author_kind="system")
            await self._tell_manager(s, r, f"{await self._label(s, landing)} is on {result.base} in {r.repo.name} already. Move the ticket this finishes to done.")
        for branch in result.conflicts:
            landing = by_branch[branch]
            landing.head_sha = landing.head_sha or ""
            await self._back(s, r, landing, f"conflicts with {result.base} or a branch that lands before it", f"Resolve the conflicts of branch {branch}", prompts.conflict_instructions(branch, result.base))
        for branch, why in [*((b, "is not on the remote any more") for b in result.missing), *((b, "still fails, and nothing new was pushed since") for b in result.unchanged)]:
            landing = by_branch[branch]
            landing.status, landing.group_no = LandingStatus.STUCK, None
            await s.commit()
            await self._tell_manager(s, r, f"Not landed in {r.repo.name}: {await self._label(s, landing)} {why}. Read the report on its ticket and decide how to go on; merge_when_green puts it on a train again.")

    # ------------------------------------------------------------------ helpers

    @staticmethod
    async def _open_repair(s: AsyncSession, landing: Landing) -> bool:
        q = select(Assignment.id).where(Assignment.landing_id == landing.id, Assignment.status.in_((AssignmentStatus.QUEUED, AssignmentStatus.RUNNING)))
        return bool(await s.scalar(q.limit(1)))

    @staticmethod
    async def _ticket(s: AsyncSession, landing: Landing) -> Task | None:
        return await s.get(Task, landing.task_id) if landing.task_id else None

    async def _label(self, s: AsyncSession, landing: Landing) -> str:
        author = await s.get(Agent, landing.agent_id) if landing.agent_id else None
        ticket = await self._ticket(s, landing)
        about = ", ".join(part for part in (author.name if author else "", f"ticket #{ticket.number}" if ticket else "") if part)
        return landing.branch + (f" ({about})" if about else "")

    async def _tell_manager(self, s: AsyncSession, r: Repo, text: str) -> None:
        manager = await agent_service.get_manager(s, r.project.id)
        await inbox.push(s, manager.id, text)
        if r.project.auto_wake_manager:
            await self._runs.request_wake(s, manager.id)
