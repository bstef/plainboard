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
- A train that fails costs no further check run to find out why. One agent gets its branches and
  the failures as one assignment and makes them pass together; a branch that needs more than a
  small fix it gives back to its author (return_branch), so the others land without it. The
  repaired branches are then checked again as a group. Searching for one guilty branch by checking
  halves does not work here: several branches fail at once, each for its own reason, and some only
  pass together. Meanwhile the next train leaves with what is new.
- After a few failed rounds the manager decides.

A repository that lands first (``repository.land_first``) does it the other way round: what is
ready is merged into the base branch at once, and the checks run on the base branch afterwards,
once for everything that landed. Their result takes nothing back. If they fail, one agent repairs
the base branch on a branch of its own, which lands like any other. The next batch lands when the
checks of the last one are over, so a check run is never replaced by a newer push.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime

from plainboard_protocol import Credentials, PullRequestCheck, PullRequestState, RepoSpec, TrainCheckSpec, TrainLandSpec, TrainResult, TrainSpec
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import ACTIVE_RUN_STATUSES, Agent, Assignment, AssignmentStatus, Landing, LandingStatus, Project, Repository, Run, Task, Train, TrainStatus, utcnow
from ..services import agents as agent_service
from ..services import board as board_service
from ..services import flow_log
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


def base_result(checks: list[PullRequestCheck], age: float) -> str:
    """The checks of a base branch that got its branches first: pending | failed | passed | none.
    none: no result will come (the repository runs no checks on a push, or a newer push replaced the run)."""
    pending = any(c.state == "pending" for c in checks)
    if age < NO_CHECKS_GRACE_SECONDS or (pending and age < landing_service.BASE_CHECK_MAX_SECONDS):
        return "pending"
    if any(c.state == "fail" for c in checks):
        return "failed"
    if not checks or pending or any(c.state == "cancelled" for c in checks):
        return "none"
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
        if train is not None and not await (self._watch_base(s, r, train, now) if train.direct else self._watch(s, r, train, now)):
            return
        await (self._land_first(s, r, now) if repo.land_first else self._depart(s, r, now))

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
        await flow_log.record(s, r.project.id, "train.blocked", train_id=train.id, detail=truncate(merged.detail, 1000))
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
        await flow_log.record(s, r.project.id, "train.aborted", train_id=train.id, branches=[m.branch for m in members])

    async def _landed(self, s: AsyncSession, r: Repo, train: Train, members: list[Landing], now: datetime) -> None:
        if train.finished_at is None:
            self._finished(train, now)
        train.status = TrainStatus.LANDED
        for member in members:
            member.status, member.merged_at, member.group_no = LandingStatus.MERGED, now, None
        await s.commit()
        await flow_log.record(s, r.project.id, "train.landed", train_id=train.id, branches=[m.branch for m in members], check_seconds=train.check_seconds)
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
        await flow_log.record(s, r.project.id, "train.stuck", train_id=train.id, branches=[m.branch for m in members], what=what)
        branches = "; ".join([await self._label(s, m) for m in members])
        await self._tell_manager(s, r, f"Not landed in {r.repo.name}: {branches} {what}. PlainBoard has stopped for these branches; fix the cause, then hand them over again with merge_when_green.")

    async def _failed(self, s: AsyncSession, r: Repo, train: Train, members: list[Landing], summary: str, evidence: str) -> None:
        """The train did not pass. One branch on it: its author repairs it. Several: one agent makes
        them pass together, and they are checked again as a group."""
        train.status = TrainStatus.FAILED
        await s.commit()
        await flow_log.record(
            s, r.project.id, "train.failed", train_id=train.id, branches=[m.branch for m in members], landing_ids=[m.id for m in members],
            repaired_group=train.group_no is not None, check_seconds=train.check_seconds, summary=summary, evidence=truncate(evidence, 4000),
        )  # fmt: skip
        if len(members) == 1:
            instructions = prompts.repair_instructions(members[0].branch, evidence, members[0].failures + 1, landing_service.MAX_FAILED_ROUNDS)
            await self._back(s, r, members[0], f"did not pass on its own ({summary})", f"Repair branch {members[0].branch}", instructions)
            return
        for member in members:
            member.failures += 1
            member.failed_sha = member.head_sha
        # A branch that has used up its rounds goes to the manager on its own: the branches it was
        # checked with did not fail that often, and are repaired like any others.
        spent = [m for m in members if m.failures >= landing_service.MAX_FAILED_ROUNDS]
        members = [m for m in members if m not in spent]
        fixer = await self._fixer(s, members) if members else None
        nobody = bool(members) and fixer is None
        if fixer is None:
            spent, members = [*spent, *members], []
        if spent:
            listed = "; ".join([await label(s, m) for m in spent])
            for member in spent:
                member.status, member.train_id, member.group_no = LandingStatus.STUCK, None, None
            await s.commit()
            await flow_log.record(s, r.project.id, "train.given_up", train_id=train.id, branches=[m.branch for m in spent], repaired=len(members), nobody_available=nobody)
            why = "and none of their authors is available" if fixer is None and not any(m.failures >= landing_service.MAX_FAILED_ROUNDS for m in spent) else f"{landing_service.MAX_FAILED_ROUNDS} times now"
            others = f" The other {len(members)} branch{'es' if len(members) != 1 else ''} of that train {'are' if len(members) != 1 else 'is'} being repaired without {'them' if len(spent) != 1 else 'it'}." if members else ""
            await self._tell_manager(
                s, r,
                f"Not landed in {r.repo.name}: {listed} did not pass{' together' if len(spent) != 1 else ''} ({summary}), {why}. PlainBoard has stopped for {'these branches' if len(spent) != 1 else 'this branch'}.{others} "
                "Decide how to go on (another approach, the failing check itself, the user); merge_when_green puts a branch on a train again.",
            )
        if not members:
            return
        if len(members) == 1:  # what is left of the train is one branch: its author repairs it
            only = members[0]
            instructions = prompts.repair_instructions(only.branch, evidence, only.failures, landing_service.MAX_FAILED_ROUNDS)
            await give_back(s, self._runs, r.repo, r.project, only, f"did not pass ({summary})", f"Repair branch {only.branch}", instructions, counts=False)
            return
        rounds = max(m.failures for m in members)
        labels = [await label(s, m) for m in members]
        group = await landing_service.next_group(s, r.repo.id)
        for member in members:
            member.status, member.group_no = LandingStatus.BACK, group  # train_id stays: it names the repair they wait for
        await s.commit()
        instructions = prompts.train_repair_instructions(r.repo.name, train.base, labels, evidence, rounds, landing_service.MAX_FAILED_ROUNDS)
        repair = await self._runs.delegate(s, None, fixer, f"Make {len(members)} branches of {r.repo.name} pass together", instructions, None, train_id=train.id)
        await flow_log.record(s, r.project.id, "train.repair_ordered", train_id=train.id, agent_id=fixer.id, assignment_id=repair.id, rounds=rounds, branches=len(members))

    @staticmethod
    async def _fixer(s: AsyncSession, members: list[Landing]) -> Agent | None:
        """Who repairs a failed train: one of its authors who has nothing to do right now, so the
        repair starts at once instead of waiting behind other work; among those, and if all are
        busy, the author of most of its branches. Until the repair is done nothing of the train lands."""
        counts: dict[int, int] = {}
        for member in members:
            if member.agent_id:
                counts[member.agent_id] = counts.get(member.agent_id, 0) + 1
        authors = [a for a in [await s.get(Agent, agent_id) for agent_id in counts] if a and can_repair(a)]
        if not authors:
            return None
        ids = [a.id for a in authors]
        busy = {
            *await s.scalars(select(Assignment.agent_id).where(Assignment.agent_id.in_(ids), Assignment.status.in_((AssignmentStatus.QUEUED, AssignmentStatus.RUNNING)))),
            *await s.scalars(select(Run.agent_id).where(Run.agent_id.in_(ids), Run.status.in_(ACTIVE_RUN_STATUSES))),
        }
        return min(authors, key=lambda a: (a.id in busy, -counts[a.id], a.id))

    async def _back(self, s: AsyncSession, r: Repo, landing: Landing, what: str, title: str, instructions: str, *, counts: bool = True) -> None:
        await give_back(s, self._runs, r.repo, r.project, landing, what, title, instructions, counts=counts)

    # ------------------------------------------------------------------ the next train

    async def _depart(self, s: AsyncSession, r: Repo, now: datetime) -> None:
        """Assemble the next train from what is ready, and start its check."""
        waiting = await self._ready(s, r)
        # Branches that were repaired together go first and on their own: what fails then is theirs.
        groups = {w.group_no for w in waiting if w.group_no is not None}
        group = min(groups) if groups else None
        # A repaired group is checked whole. New branches: as many as land per check run (see train_size).
        size = landing_service.MAX_TRAIN if group is not None else await landing_service.train_size(s, r.repo.id)
        pick = [w for w in waiting if w.group_no == group][:size]
        await s.commit()
        if not pick:
            return
        if group is not None and await self._unrepaired(s, r, pick):
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
            # A group failed together: a branch of it that needed no change still belongs on the train.
            skip_unchanged={p.branch: p.failed_sha for p in pick if p.failed_sha} if group is None else {},
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
        await flow_log.record(
            s, r.project.id, "train.departed" if on_train else "train.empty", train_id=train.id if on_train else None, branches=[p.branch for p in on_train],
            ready=len(waiting), size_limit=size, repaired_group=group is not None, mode=r.mode, landed_already=list(result.landed), conflicts=list(result.conflicts),
            missing=list(result.missing), unchanged=list(result.unchanged),
        )  # fmt: skip

        await self._left_out(s, r, result, by_branch, tickets, now)

    async def _ready(self, s: AsyncSession, r: Repo) -> list[Landing]:
        """What can go to the base branch next: handed over, or repaired since it came back."""
        ready = select(Landing).where(Landing.repository_id == r.repo.id)
        for landing in list(await s.scalars(ready.where(Landing.status == LandingStatus.BACK))):
            if not await open_repair(s, landing):  # repaired: it goes on by itself
                landing.status = LandingStatus.WAITING
        await s.commit()
        waiting = list(await s.scalars(ready.where(Landing.status == LandingStatus.WAITING).order_by(Landing.id)))
        for landing in [w for w in waiting if not w.branch]:  # handed over as a pull request number, before trains
            state = await self._runs.runner.pull_request(r.credentials, r.slug, landing.number) if r.slug and landing.number else None
            landing.branch = state.branch if state and state.state == "open" else ""
            if not landing.branch:
                landing.status = LandingStatus.CLOSED
        await s.commit()
        return [w for w in waiting if w.branch]

    async def _left_out(self, s: AsyncSession, r: Repo, result: TrainResult, by_branch: dict[str, Landing], tickets: dict[int, Task | None], now: datetime) -> None:
        """What could not be merged with the others."""
        for branch in result.landed:
            landing = by_branch[branch]
            landing.status, landing.merged_at, landing.group_no = LandingStatus.MERGED, now, None
            await s.commit()
            if ticket := tickets[landing.id]:
                await board_service.add_comment(s, ticket, f"Branch {branch} is on {result.base} already.", author_kind="system")
            await self._tell_manager(s, r, f"{await self._label(s, landing)} is on {result.base} in {r.repo.name} already. Move the ticket this finishes to done.")
        for branch in result.conflicts:
            landing = by_branch[branch]
            # A conflict is no failed check: it does not use up a round. Unchanged, it is not tried again.
            landing.failed_sha = landing.head_sha or ""
            await self._back(
                s, r, landing, f"conflicts with {result.base} or a branch that lands before it", f"Resolve the conflicts of branch {branch}", prompts.conflict_instructions(branch, result.base), counts=False
            )  # fmt: skip
        for branch, why in [*((b, "is not on the remote any more") for b in result.missing), *((b, "still fails, and nothing new was pushed since") for b in result.unchanged)]:
            landing = by_branch[branch]
            landing.status, landing.group_no = LandingStatus.STUCK, None
            await s.commit()
            await flow_log.record(s, r.project.id, "landing.stuck", landing_id=landing.id, task_id=landing.task_id, branch=branch, why=why)
            await self._tell_manager(s, r, f"Not landed in {r.repo.name}: {await self._label(s, landing)} {why}. Read the report on its ticket and decide how to go on; merge_when_green puts it on a train again.")

    # ------------------------------------------------------------------ land first, check after

    async def _land_first(self, s: AsyncSession, r: Repo, now: datetime) -> None:
        """Merge everything that is ready into the base branch now. The checks run on the base
        branch afterwards (_watch_base) and take nothing back."""
        waiting = await self._ready(s, r)
        pick = waiting[: landing_service.MAX_BATCH]
        if not pick or (self.linger > 0 and (now - min(p.updated_at for p in pick)).total_seconds() < self.linger):
            return
        train = Train(project_id=r.project.id, repository_id=r.repo.id, mode=r.mode or landing_service.UNCHECKED, direct=True, status=TrainStatus.CHECKING, created_at=now)
        s.add(train)
        await s.flush()
        tickets = {p.id: await self._ticket(s, p) for p in pick}
        spec = TrainSpec(
            credentials=r.credentials,
            key=r.key,
            remote=r.remote,
            base=r.repo.default_branch or None,
            branch=f"plainboard/land-{train.id}",
            members=[p.branch for p in pick],
            skip_unchanged={p.branch: p.failed_sha for p in pick if p.failed_sha},
        )
        result = await self._runs.runner.assemble_train(spec)
        by_branch = {p.branch: p for p in pick}
        landed = [by_branch[b] for b in result.merged if b in by_branch]
        pushed = None
        if landed:
            heads = {m.branch: result.merged[m.branch] for m in landed}
            pushed = await self._runs.runner.land_train(TrainLandSpec(credentials=r.credentials, key=r.key, remote=r.remote, base=result.base, head_sha=result.head_sha, delete_branches=heads))
        if pushed is None or not pushed.ok:
            await s.delete(train)
            await s.commit()
            if pushed is not None and base_moved(pushed.detail):
                return  # someone else pushed to the base branch meanwhile: merged again with the next look
            if pushed is not None:
                for landing in landed:
                    landing.status, landing.group_no = LandingStatus.STUCK, None
                await s.commit()
                listing = "; ".join([await label(s, m) for m in landed])
                await flow_log.record(s, r.project.id, "train.stuck", branches=[m.branch for m in landed], what=truncate(pushed.detail, 300))
                await self._tell_manager(
                    s, r, f"Not landed in {r.repo.name}: {listing} could not be pushed to {result.base}: {truncate(pushed.detail, 300)} Fix the cause, then hand them over again with merge_when_green."
                )
        else:
            for landing in landed:
                landing.status, landing.train_id, landing.head_sha, landing.merged_at, landing.group_no = LandingStatus.MERGED, train.id, result.merged[landing.branch], now, None
            train.branch, train.base, train.head_sha, train.size = spec.branch, result.base, result.head_sha, len(landed)
            if r.mode == landing_service.COMMAND:
                train.job_id = secrets.token_hex(8)
                await self._runs.runner.check_train(TrainCheckSpec(credentials=r.credentials, key=r.key, id=train.job_id, command=r.repo.check_command.strip()))
            elif r.mode is None:
                train.status, train.finished_at = TrainStatus.LANDED, now  # nothing checks this repository
            await s.commit()
            timing_service.forget_cache()
            await flow_log.record(
                s, r.project.id, "train.landed", train_id=train.id, branches=[m.branch for m in landed], direct=True, ready=len(waiting), mode=train.mode,
                conflicts=list(result.conflicts), missing=list(result.missing), unchanged=list(result.unchanged),
            )  # fmt: skip
            for landing in landed:
                if ticket := tickets[landing.id]:
                    await board_service.add_comment(s, ticket, f"Branch {landing.branch} is on {result.base}.", author_kind="system")
            listing = "; ".join([await label(s, m) for m in landed])
            checks = "This repository has no checks." if r.mode is None else f"Its checks run on {result.base} now, once for all of this; PlainBoard has a failure repaired."
            await self._tell_manager(s, r, f"Landed on {result.base} in {r.repo.name}: {listing}. {checks} Move the tickets this finishes to done.")
        await self._left_out(s, r, result, by_branch, tickets, now)

    async def _watch_base(self, s: AsyncSession, r: Repo, train: Train, now: datetime) -> bool:
        """A batch that landed before its checks: look at the checks of the base branch at that
        commit. True once they are over or will not come, so the next batch can land."""
        failed: list[str] = []
        links: dict[str, str] = {}
        evidence = ""
        if train.mode == landing_service.COMMAND:
            state = (await self._runs.runner.job_status([train.job_id or ""])).get(train.job_id or "")
            if state is not None and state.running:
                return False
            result = "none" if state is None else "passed" if state.exit_code == 0 else "failed"
            if state is not None and result == "failed":
                failed = ["check command"]
                evidence = f"Check command: {r.repo.check_command.strip()}\nIt ended with exit code {state.exit_code}. Last output:\n{r.redact(state.tail)}\nFull log: {state.log_path}"  # type: ignore[operator]
        else:
            checks = await self._runs.runner.commit_checks(r.credentials, r.slug, train.head_sha) if r.slug else []
            result = base_result(checks, (now - train.created_at).total_seconds())
            if result == "pending":
                return False
            links = {c.name: c.link for c in checks if c.state == "fail"}
            failed = list(links)
            evidence = self._failed_checks(train, links)
        self._finished(train, now)
        train.status = {"failed": TrainStatus.FAILED, "passed": TrainStatus.LANDED}.get(result, TrainStatus.ABORTED)
        train.failed_checks = "\n".join(failed)
        await s.commit()
        await flow_log.record(s, r.project.id, {"failed": "base.red", "passed": "base.green"}.get(result, "base.unchecked"), train_id=train.id, checks=failed, check_seconds=train.check_seconds)
        if result == "failed":
            await self._base_red(s, r, train, failed, evidence, links)
        return True

    @staticmethod
    def _failed_checks(train: Train, links: dict[str, str]) -> str:
        listed = "\n".join(f"- {name}" + (f": {link}" if link else "") for name, link in list(links.items())[:20])
        return f"Failed checks of {train.base} at {train.head_sha[:10]}:\n{listed}\nRead the failed logs once: the links, or gh run view <run id> --log-failed."

    async def _base_red(self, s: AsyncSession, r: Repo, train: Train, failed: list[str], evidence: str, links: dict[str, str]) -> None:
        """The base branch does not pass its checks with what landed. Nothing is taken back: one
        agent repairs it on a branch of its own, which lands like any other. A failure that is being
        repaired already, or whose fix has not landed yet, orders nothing more; what failed in
        addition gets a repair of its own, without the failures the other one has."""
        streak = await landing_service.red_streak(s, train)
        ids = [t.id for t in streak]
        repairs = list(await s.scalars(select(Assignment).where(Assignment.train_id.in_(ids)).order_by(Assignment.id.desc()))) if ids else []
        known = {name for t in streak for name in t.failed_checks.split("\n")}
        if repairs:
            last = repairs[0]
            on_its_way = select(Landing.id).where(
                Landing.repository_id == r.repo.id, Landing.agent_id == last.agent_id, Landing.status.in_(landing_service.ACTIVE), Landing.updated_at >= last.created_at
            )
            # Its fix could not be in this batch yet: what it repairs is not ordered again.
            if last.status in (AssignmentStatus.QUEUED, AssignmentStatus.RUNNING) or (last.finished_at and last.finished_at > train.created_at) or await s.scalar(on_its_way.limit(1)):
                more = [name for name in failed if name not in known]
                if not more:
                    await flow_log.record(s, r.project.id, "base.still_red", train_id=train.id, assignment_id=last.id)
                    return
                if links:  # only where the checks have names of their own
                    being_fixed = [name for name in failed if name in known]
                    evidence = self._failed_checks(train, {name: links.get(name, "") for name in more}) + (
                        f"\nThese failed too, and assignment #{last.id} repairs them (its fix has not landed yet): {', '.join(being_fixed[:12])}. Leave them: what they have in common with yours is fixed there."
                        if being_fixed
                        else ""
                    )
                    failed = more
        if set(failed) <= known and any(t.escalated for t in streak):
            return  # the manager has it
        members = list(await s.scalars(select(Landing).where(Landing.train_id == train.id).order_by(Landing.id)))
        fixer = await self._fixer(s, members) or (await self._fixer(s, list(await s.scalars(select(Landing).where(Landing.train_id.in_(ids))))) if ids else None)
        names = ", ".join(failed[:8]) + (f" and {len(failed) - 8} more" if len(failed) > 8 else "")
        if fixer is None or len(repairs) >= landing_service.MAX_FAILED_ROUNDS:
            train.escalated = True
            await s.commit()
            why = "and nobody who wrote what landed is available" if fixer is None else f"and {len(repairs)} repairs have not made it pass"
            await flow_log.record(s, r.project.id, "base.escalated", train_id=train.id, repairs=len(repairs))
            await self._tell_manager(
                s, r,
                f"{train.base} of {r.repo.name} does not pass its checks ({names}), {why}. Nothing was taken back: what landed is on {train.base}. "
                "Decide how to go on: delegate the repair with what you know, or change the check that keeps failing.",
            )
            return
        branches = [await label(s, m) for m in members]
        instructions = prompts.base_red_instructions(r.repo.name, train.base, branches, evidence, len(repairs) + 1, landing_service.MAX_FAILED_ROUNDS)
        repair = await self._runs.delegate(s, None, fixer, f"Make {train.base} of {r.repo.name} pass its checks again", instructions, None, train_id=train.id)
        await flow_log.record(s, r.project.id, "base.repair_ordered", train_id=train.id, agent_id=fixer.id, assignment_id=repair.id, round=len(repairs) + 1, checks=failed)

    async def _unrepaired(self, s: AsyncSession, r: Repo, pick: list[Landing]) -> bool:
        """Whether a group comes back from its repair exactly as it failed: the same branches, none
        of them changed. Checking that again would fail again, so the manager decides."""
        failed = await s.get(Train, pick[0].train_id) if pick[0].train_id else None
        if failed is None or failed.size != len(pick):
            return False  # a branch was taken off: the others may pass without it
        for landing in pick:
            if await self._runs.runner.branch_head(r.credentials, r.remote, landing.branch) != landing.failed_sha:
                return False
        for landing in pick:
            landing.status, landing.train_id, landing.group_no = LandingStatus.STUCK, None, None
        await s.commit()
        await flow_log.record(s, r.project.id, "train.unrepaired", train_id=failed.id, branches=[p.branch for p in pick])
        branches = "; ".join([await label(s, p) for p in pick])
        await self._tell_manager(
            s, r,
            f"Not landed in {r.repo.name}: {branches} did not pass together, and their repair changed none of them. PlainBoard has stopped for these "
            "branches. Read the repair's report (list_assignments): if the cause is outside the branches (a check that fails at random, a broken "
            f"{failed.base}), get that fixed; merge_when_green puts a branch on a train again.",
        )
        return True

    # ------------------------------------------------------------------ helpers

    @staticmethod
    async def _ticket(s: AsyncSession, landing: Landing) -> Task | None:
        return await s.get(Task, landing.task_id) if landing.task_id else None

    async def _label(self, s: AsyncSession, landing: Landing) -> str:
        return await label(s, landing)

    async def _tell_manager(self, s: AsyncSession, r: Repo, text: str) -> None:
        await tell_manager(s, self._runs, r.project, text)


def can_repair(agent: Agent) -> bool:
    """Whether a branch can go back to this agent. A manager does not repair. An agent that is paused
    with the team can: the assignment waits for it and starts when the team goes on."""
    return not agent.is_manager and (agent.enabled or agent.team_pause)


async def open_repair(s: AsyncSession, landing: Landing) -> bool:
    """Whether someone is repairing this branch right now: on its own, or with the train it failed on."""
    mine = Assignment.landing_id == landing.id
    if landing.train_id is not None:
        mine = or_(mine, Assignment.train_id == landing.train_id)
    q = select(Assignment.id).where(mine, Assignment.status.in_((AssignmentStatus.QUEUED, AssignmentStatus.RUNNING)))
    return bool(await s.scalar(q.limit(1)))


async def label(s: AsyncSession, landing: Landing) -> str:
    author = await s.get(Agent, landing.agent_id) if landing.agent_id else None
    ticket = await s.get(Task, landing.task_id) if landing.task_id else None
    about = ", ".join(part for part in (author.name if author else "", f"ticket #{ticket.number}" if ticket else "") if part)
    name = landing.title if (landing.title or "").startswith(f"{landing.branch} carrying ") else landing.branch  # a branch that carries others says which
    return name + (f" ({about})" if about else "")


async def tell_manager(s: AsyncSession, runs: RunManager, project: Project, text: str) -> None:
    manager = await agent_service.get_manager(s, project.id)
    await inbox.push(s, manager.id, text)
    if project.auto_wake_manager:
        await runs.request_wake(s, manager.id)


async def give_back(
    s: AsyncSession, runs: RunManager, repo: Repository, project: Project, landing: Landing, what: str, title: str, instructions: str, *, counts: bool = True
) -> bool:
    """Give a branch back to its author as one assignment; after a few rounds the manager decides.
    False if it went to the manager. ``counts``: this is a failed round of its own (not one that was
    counted with its train already)."""
    if counts:
        landing.failures += 1
        landing.failed_sha = landing.head_sha
    landing.train_id, landing.group_no = None, None
    author = await s.get(Agent, landing.agent_id) if landing.agent_id else None
    ticket = await s.get(Task, landing.task_id) if landing.task_id else None
    available = author is not None and can_repair(author)
    if landing.failures >= landing_service.MAX_FAILED_ROUNDS or not available:
        landing.status = LandingStatus.STUCK
        await s.commit()
        await flow_log.record(s, project.id, "landing.stuck", landing_id=landing.id, task_id=landing.task_id, branch=landing.branch, why=what, failures=landing.failures)
        if available:
            why = f"{landing.failures} times now"
        elif author is None or author.is_manager:
            why = "and it has no author who could repair it (you handed it over): delegate that to the member who knows it"
        else:
            why = f"and its author {author.name} is switched off"
        await tell_manager(
            s, runs, project,
            f"Not landed in {repo.name}: {await label(s, landing)} {what}, {why}. PlainBoard has stopped for this branch. "
            "Decide how to go on (another approach, the failing check itself, the user); merge_when_green puts it on a train again.",
        )
        return False
    landing.status = LandingStatus.BACK
    await s.commit()
    await flow_log.record(s, project.id, "landing.back", landing_id=landing.id, task_id=landing.task_id, agent_id=author.id, branch=landing.branch, why=what, failures=landing.failures)
    if not await open_repair(s, landing):  # the repair is an assignment on the ticket: no comment says it again
        await runs.delegate(s, None, author, title, instructions, ticket, landing_id=landing.id)
    return True
