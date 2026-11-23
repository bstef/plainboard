"""External runners update themselves to the app's version, each on its own, once it is free
(services/runner_update.py decides, the runner's selfupdate.py takes it). Looked at with the job
watcher's look (``RunManager.check_runners``).

A runner with another version is sent the app's code first (it checks and unpacks it; nothing that
runs is touched), then drained: no new turn or job starts there (they wait in the queue, as while a
runner is away; turns asked of an agent by a running turn still start, no train leaves), and running
turns, jobs, servers and check runs get the setting ``runner_update_drain_seconds`` (5 minutes) to end.
What still runs then ends with the restart: a cut turn is lost (RunManager._events), its assignment goes
on in the same session once the runner is back; the managers hear of cut jobs, the job watcher reports
them lost; a train's check run is given up without a result and runs again. Then the runner restarts
into the new code; its launcher puts the old one back when the new one does not come up. A runner that
can't take the update, or one whose update failed, is left as it is: the managers hear it once
(``Runner.update_told``), and the same version is not tried again for ``RETRY_SECONDS``."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from ..models import Job, JobStatus, Run, RunStatus, Runner, Train, TrainStatus
from ..services import runner_update
from ..services import settings as settings_service
from ..services import version as version_service
from ..services.runners import short
from . import inbox
from .runner_client import UpdateRefused

if TYPE_CHECKING:
    from .run_manager import RunManager

log = logging.getLogger(__name__)
STAGE_SECONDS = 90  # the code is small; a runner that takes longer is not well


class RunnerUpdates:
    def __init__(self, runs: RunManager) -> None:
        self._runs = runs
        self._draining: dict[int, float] = {}  # runner -> staged then (monotonic): nothing new starts there
        self._blocked: dict[int, float] = {}  # runner -> no try before (monotonic)
        self._restarted: dict[int, str] = {}  # runner -> the version it restarted into
        self._task: asyncio.Task[None] | None = None

    def poke(self) -> None:
        """A look in the background: sending the code may take a moment, the job watcher goes on meanwhile."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.look(), name="runner-updates")

    def connected(self, runner_id: int) -> None:
        """A runner connected: a drain ends (a restart is a new runner; it is looked at again)."""
        self._draining.pop(runner_id, None)
        self._runs.runners.updating.discard(runner_id)
        self._runs.runners.restart_by.pop(runner_id, None)

    async def look(self) -> None:
        own = version_service.running_commit()
        hub = self._runs.runners
        for runner_id in list(hub.tunnels):
            if not hub.connected(runner_id):
                continue
            try:
                await self._look(runner_id, own)
            except Exception:
                log.exception("Update of runner %s not looked at", runner_id)

    async def _limit(self) -> float:
        async with self._runs._sm() as s:
            value = await settings_service.get(s, "runner_update_drain_seconds")
        return max(float(runner_update.DRAIN_SECONDS if value is None else value), 0.0)

    async def _busy(self, runner_id: int) -> tuple[int, dict[int, int]]:
        """Running turns of the runner's projects, and per project its running jobs and the check runs it runs."""
        projects = self._runs.runners.projects_of(runner_id) or set()
        if not projects:
            return 0, {}
        async with self._runs._sm() as s:
            turns = await s.scalar(select(func.count(Run.id)).where(Run.project_id.in_(projects), Run.status == RunStatus.RUNNING)) or 0
            jobs: dict[int, int] = {}
            for pid, n in (await s.execute(select(Job.project_id, func.count(Job.id)).where(Job.project_id.in_(projects), Job.status == JobStatus.RUNNING).group_by(Job.project_id))).all():
                jobs[pid] = jobs.get(pid, 0) + n
            for pid, n in (await s.execute(select(Train.project_id, func.count(Train.id)).where(Train.project_id.in_(projects), Train.status == TrainStatus.CHECKING, func.coalesce(Train.job_id, "") != "").group_by(Train.project_id))).all():
                jobs[pid] = jobs.get(pid, 0) + n
        return turns, jobs

    async def _look(self, runner_id: int, own: str) -> None:
        hub = self._runs.runners
        hello = hub.hello.get(runner_id) or {}
        version = str(hello.get("version") or "")
        now = time.monotonic()
        reason = ""
        failed = (hello.get("update") or {}).get("failed") or {}
        if failed and short(str(failed.get("commit") or "")) == short(own) and time.time() - float(failed.get("at") or 0) < runner_update.RETRY_SECONDS:
            reason = str(failed.get("reason") or "it did not come back")
        elif self._restarted.get(runner_id) == own and short(version) != short(own):
            reason = "it came back with its old version"
        draining = self._draining.get(runner_id)
        turns, jobs = await self._busy(runner_id) if draining is not None else (0, {})
        limit = await self._limit() if draining is not None else runner_update.DRAIN_SECONDS
        blocked = self._blocked.get(runner_id, 0) > now
        what = runner_update.step(own, version, bool(reason), blocked, draining, turns, sum(jobs.values()), now, limit)
        if what == runner_update.FAILED:
            self._restarted.pop(runner_id, None)
            self._blocked[runner_id] = now + runner_update.RETRY_SECONDS
            await self._tell(runner_id, f"failed:{own}", lambda name: runner_update.failed_note(name, own, reason))
        elif what == runner_update.STAGE:
            await self._stage(runner_id, own, version)
        elif what == runner_update.RESTART:
            await self._restart(runner_id, own, jobs, limit)

    async def _stage(self, runner_id: int, own: str, version: str) -> None:
        bundle = await asyncio.to_thread(runner_update.bundle, own)
        if bundle is None:
            return  # this app has no runner code to give (a build without it)
        now = time.monotonic()
        try:
            await self._runs.runners.client(runner_id).stage_update(bundle.data, bundle.sha256, bundle.commit, timeout=STAGE_SECONDS)
        except UpdateRefused as exc:
            self._blocked[runner_id] = now + runner_update.RETRY_SECONDS
            why = str(exc)
            log.warning("Runner %s can't update itself: %s", runner_id, why)
            await self._tell(runner_id, f"manual:{own}", lambda name: runner_update.manual_note(name, version, own, why))
            return
        except Exception as exc:  # it did not arrive whole, or the line broke
            self._blocked[runner_id] = now + runner_update.RETRY_SECONDS
            why = str(exc)[:200] or type(exc).__name__
            log.warning("Update not sent to runner %s: %s", runner_id, why)
            await self._tell(runner_id, f"failed:{own}", lambda name: runner_update.failed_note(name, own, why))
            return
        log.info("Runner %s took the update to %s: it runs idle now", runner_id, own[:10])
        self._draining[runner_id] = now
        self._runs.runners.updating.add(runner_id)
        self._runs.runners.restart_by[runner_id] = time.time() + await self._limit()  # shown: it restarts at the latest then

    async def _restart(self, runner_id: int, own: str, jobs: dict[int, int], limit: float) -> None:
        try:
            await self._runs.runners.client(runner_id).restart_update(own)
        except Exception as exc:  # e.g. it restarted since and has nothing staged: sent again
            log.info("Runner %s did not restart for the update: %s", runner_id, exc)
            self.connected(runner_id)
            self._runs.wake()  # what waited for it starts
            return
        self._restarted[runner_id] = own
        if jobs:
            async with self._runs._sm() as s:
                runner = await s.get(Runner, runner_id)
                for manager in await self._runs._managers_on(s, runner_id):
                    if n := jobs.get(manager.project_id):
                        await inbox.push(s, manager.id, runner_update.cut_note(runner.name if runner else "?", n, limit))
                await s.commit()
        log.warning("Runner %s restarts into %s", runner_id, own[:10])

    async def _tell(self, runner_id: int, key: str, note) -> None:
        """The managers of the runner's projects, once per ``key``."""
        async with self._runs._sm() as s:
            runner = await s.get(Runner, runner_id)
            if runner is None or runner.update_told == key[:80]:
                return
            runner.update_told = key[:80]
            await s.commit()
            for manager in await self._runs._managers_on(s, runner_id):
                await inbox.push(s, manager.id, note(runner.name))
                await self._runs.request_wake(s, manager.id)
