from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import accounts, auth, browser, chat, internal, knowledge, preview, projects, public, repositories, secrets, system, tasks, todos, ws
from .config import get_config
from .db import dispose_engine, init_engine, sessionmaker
from .migrate import upgrade
from .orchestration.run_manager import RunManager
from .orchestration.runner_client import RunnerClient
from .orchestration.job_watcher import JobWatcher
from .orchestration.landing import LandingWatcher
from .orchestration.limits_watcher import LimitsWatcher
from .orchestration.update_watcher import UpdateWatcher
from .orchestration.scheduler import Scheduler
from .models import utcnow
from .security import init_security
from .services import version as version_service
from .services.errors import ServiceError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("plainboard")

PUBLIC_PREFIXES = ("/api/auth/", "/api/internal/", "/api/public/", "/api/health")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = get_config()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.uploads_dir.mkdir(parents=True, exist_ok=True)
    init_security(config.data_dir)
    await asyncio.to_thread(upgrade)
    init_engine(config.database_url)
    async with sessionmaker()() as session:
        await version_service.note_start(session, utcnow())
    runner = RunnerClient(config.runner_url)
    runs = RunManager(sessionmaker(), runner)
    scheduler = Scheduler(sessionmaker(), runs, config.scheduler_interval_seconds)
    job_watcher = JobWatcher(sessionmaker(), runs, config.job_watch_interval_seconds)
    landing = LandingWatcher(sessionmaker(), runs, config.landing_interval_seconds, config.landing_linger_seconds)
    app.state.runner, app.state.runs, app.state.scheduler, app.state.job_watcher = runner, runs, scheduler, job_watcher
    app.state.landing = landing
    limits_watcher = LimitsWatcher(runs, config.limits_interval_seconds)
    updates = app.state.updates = UpdateWatcher(sessionmaker(), runner, config.update_interval_seconds)
    app.state.preview_client = preview.new_client()
    await runs.start()
    scheduler.start()
    job_watcher.start()
    landing.start()
    limits_watcher.start()
    updates.start()
    log.info("PlainBoard ready (auth %s)", "on" if config.password else "off")
    try:
        yield
    finally:
        await updates.stop()
        await limits_watcher.stop()
        await landing.stop()
        await job_watcher.stop()
        await scheduler.stop()
        await runs.stop()
        await runner.aclose()
        await app.state.preview_client.aclose()
        await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(title="PlainBoard", lifespan=lifespan)

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        # First: a preview page's requests belong to the previewed server, whatever their path.
        if redirect := preview.redirect_from_preview_page(request):
            return redirect
        if path.startswith("/api/") and not path.startswith(PUBLIC_PREFIXES) and not auth.is_authenticated(request):
            if refusal := await auth.read_key_refusal(request):  # no login: a read key may still read
                return JSONResponse({"detail": refusal[1]}, status_code=refusal[0])
        return await call_next(request)

    @app.exception_handler(ServiceError)
    async def service_error(_request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    for module in (auth, system, accounts, projects, public, repositories, secrets, tasks, todos, chat, knowledge, internal, ws, browser, preview):
        app.include_router(module.router)

    _mount_spa(app, get_config().static_dir)
    return app


def _mount_spa(app: FastAPI, static_dir: Path) -> None:
    index = static_dir / "index.html"
    if not index.exists():
        log.warning("No frontend build found in %s", static_dir)
        return
    if (static_dir / "assets").exists():
        app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise ServiceError("Not found", 404)
        # Revalidate: an installed app must pick up a new build, and must notice when the server is gone.
        headers = {"Cache-Control": "no-cache"}
        candidate = (static_dir / path).resolve()
        if path and candidate.is_file() and static_dir.resolve() in candidate.parents:
            return FileResponse(candidate, headers=headers)
        return FileResponse(index, headers=headers)


app = create_app()
