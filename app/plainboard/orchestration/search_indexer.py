"""Keeps the semantic search index current, without a model turn and without holding anything up.

Every ``search_interval_seconds`` it reads, per project with search on, what each indexer's source
changed since its cursor (the first time: everything, in batches, which is the backfill), brings
those chunks up to date (unchanged text keeps its vector) and embeds what waits, a batch at a time,
newest first. While there is more it goes on after a short pause, else it waits for the next look.
Commits come from the runner (``/git/log``), per repository from its last indexed commit, at most every
``COMMITS_EVERY``. Every ``SWEEP_EVERY`` what was deleted at its source leaves the index.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from plainboard_protocol import GitLogSpec
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import Project, Repository, SearchCursor, SearchDocument, utcnow
from ..services import landings as landing_service
from ..services import permissions
from ..services import search
from ..services.search import indexers, store
from .run_manager import RunManager
from .runner_client import RunnerError

log = logging.getLogger(__name__)

BATCH = 200  # items per indexer and look
EMBED_BATCH = 16  # chunks per call of the model: more is hardly faster on two threads, and needs more memory
EMBED_ROUNDS = 8  # calls per look: then the others get the CPU and the database again
BUSY_PAUSE = 2.0  # seconds between looks while there is more to do
COMMITS_EVERY = timedelta(minutes=10)
SWEEP_EVERY = timedelta(minutes=30)
COMMITS_FIRST = 1000  # the newest commits of a repository the first time


class SearchIndexer:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], runs: RunManager, interval: float):
        self._sm = sessionmaker
        self._runs = runs
        self._interval = interval
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="search-indexer")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        await asyncio.sleep(self._interval)  # not in the middle of the start
        while True:
            try:
                more = await self.tick()
            except Exception:
                log.exception("Search indexing failed")
                more = False
            await asyncio.sleep(BUSY_PAUSE if more else self._interval)

    async def tick(self, now: datetime | None = None) -> bool:
        """One look; True while there is more to index or embed."""
        now = now or utcnow()
        if self._runs.paused:
            return False
        async with self._sm() as s:
            projects = await search.active_projects(s)
            if not projects:
                return False
            more = False
            for project in projects:
                more |= await self._index(s, project)
                await self._sweep(s, project, now)
                await self._commits(s, project, now)
            return await self._embed(s, [p.id for p in projects]) or more

    async def _cursor(self, s: AsyncSession, project_id: int, source: str) -> SearchCursor:
        cursor = await s.get(SearchCursor, (project_id, source))
        if cursor is None:
            cursor = SearchCursor(project_id=project_id, source=source, value="", checked_at=datetime.min)
            s.add(cursor)
        return cursor

    async def _index(self, s: AsyncSession, project: Project) -> bool:
        more = False
        for indexer in indexers.INDEXERS:
            cursor = await self._cursor(s, project.id, indexer.kind)
            items = await indexer.changed(s, project.id, cursor.value, BATCH)
            if items:
                await store.put(s, project.id, project.name, indexer.kind, [(source_id, doc) for _, source_id, doc in items])
                cursor.value = items[-1][0]
                more |= len(items) == BATCH
            await s.commit()
        return more

    async def _sweep(self, s: AsyncSession, project: Project, now: datetime) -> None:
        cursor = await self._cursor(s, project.id, "sweep")
        if cursor.checked_at > now - SWEEP_EVERY:
            return
        for indexer in indexers.INDEXERS:
            await store.remove_missing(s, project.id, indexer.kind, indexer.existing(project.id))
        await s.execute(delete(SearchDocument).where(indexers.stale_commits(project.id)))
        cursor.checked_at = now
        await s.commit()

    async def _commits(self, s: AsyncSession, project: Project, now: datetime) -> None:
        if not self._runs.runners.ready(project.id):
            return  # an external runner that is away
        for repo in list(await s.scalars(select(Repository).where(Repository.project_id == project.id).order_by(Repository.id))):
            cursor = await self._cursor(s, project.id, f"commit:{repo.id}")
            if cursor.checked_at > now - COMMITS_EVERY:
                continue
            spec = GitLogSpec(
                credentials=await permissions.credentials(s, project),
                key=landing_service.train_key(repo),
                remote=landing_service.remote(repo),
                base=repo.default_branch or None,
                since=cursor.value.split(" ")[0] or None,
                limit=COMMITS_FIRST,
            )
            cursor.checked_at = now  # also after a failure: tried again later, not with every look
            await s.commit()  # nothing waits on the database while the runner works
            try:
                result = await self._runs.runners.of(project.id).git_log(spec)
            except (RunnerError, AttributeError) as exc:  # AttributeError: a runner client without it
                log.info("Reading the commits of repository %s failed: %s", repo.id, exc)
                continue
            docs = indexers.commit_docs(repo, result.branch, result.commits)
            await store.put(s, project.id, project.name, indexers.COMMIT, [(d.source_id, d) for d in docs])
            cursor = await self._cursor(s, project.id, f"commit:{repo.id}")
            cursor.value = f"{result.head} {result.branch}"  # where to go on from, and the branch it is on
            await s.commit()

    async def _embed(self, s: AsyncSession, project_ids: list[int]) -> bool:
        """The current model first (search stays current), then the one the index is being made anew for;
        once that one has every chunk, search switches to it."""
        more = False
        for model in [await search.embedder(s), await search.next_embedder(s)]:
            if model is not None:
                more |= await self._embed_with(s, model, project_ids)
        if not more and await search.switch_when_complete(s, project_ids):
            await s.commit()
        return more

    async def _embed_with(self, s: AsyncSession, model, project_ids: list[int]) -> bool:
        for _ in range(EMBED_ROUNDS):
            rows = await store.pending(s, model.name, project_ids, EMBED_BATCH)
            await s.commit()
            if not rows:
                return False
            if model.state != "ready":
                model.prepare()  # downloads and loads in the background; meanwhile search finds by words
                return False
            vectors = await model.passages([text for _, _, text, _ in rows])
            await store.store_vectors(s, model.name, rows, vectors)
            await s.commit()
            if len(rows) < EMBED_BATCH:
                return False
        return True
