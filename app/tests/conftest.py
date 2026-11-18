from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from plainboard_protocol import (
    LimitsEvent,
    LimitWindow,
    AssistantTextEvent,
    CheckResult,
    CompactionEvent,
    CompletionResult,
    CompletionSpec,
    JobSpec,
    JobState,
    ModelInfo,
    ProviderInfo,
    PullRequestCheck,
    PullRequestState,
    ResultEvent,
    RunEvent,
    RunSpec,
    SessionEvent,
    ToolResultEvent,
    ToolUseEvent,
    TidyResult,
    TrainResult,
    UsageEvent,
)

from plainboard.orchestration.runner_client import RunnerError, RunnerUnavailable


class FakeRunner:
    """Stands in for the runner service. Understands the same directives as the mock provider:
    ``TOOL <name> <json>`` and ``SAY <text>``; ``CONTEXT <n>`` sets the reported context size,
    ``COMPACT <n>`` reports a compaction by the provider."""

    app = None  # set by fixture; used to execute tools through the real RunManager

    def browser_url(self, key: str) -> str:
        return f"ws://runner/browser/{key}"

    def __init__(self, base_url: str):
        self.specs: list[RunSpec] = []
        self.completions: list[CompletionSpec] = []
        self.notices: list[tuple[int, str]] = []
        self.limit_windows: list[LimitWindow] = []
        self.limit_calls: list = []
        self.unavailable = 0  # how many starts of a turn fail because the runner is not up yet
        self.jobs: dict[str, JobState] = {}
        self.job_specs: list[JobSpec] = []
        self.ports: dict[str, list] = {}  # job id -> the web servers it has started (JobPort)
        self.reachable = True
        self.released = False
        # Landing: what the git host would say.
        self.branches: dict[str, str] = {}  # branch -> head commit on the remote
        self.conflicting: set[str] = set()  # branches that cannot be merged onto the base
        self.commit_checks_of: dict[str, list] = {}  # commit -> the checks GitHub ran for it on the base branch
        self.on_base: set[str] = set()  # branches the base branch already contains
        self.train_specs: list = []
        self.train_results: list[TrainResult] = []
        self.check_specs: list = []
        self.land_specs: list = []
        self.land_error = ""
        self.pulls: dict[tuple[str, int], PullRequestState] = {}  # GitHub: the trains' pull requests
        self.merged: list[tuple[str, int, dict]] = []
        self.closed: list[int] = []
        self.merge_error = ""
        # Newer versions of PlainBoard itself: what GitHub would say.
        self.upstream_state = None
        self.upstream_error = ""
        self.upstream_calls: list[tuple[str, str, str]] = []

    async def stream_run(self, spec: RunSpec) -> AsyncIterator[RunEvent]:
        if self.unavailable > 0:  # not up yet
            self.unavailable -= 1
            raise RunnerUnavailable("Runner unreachable: [Errno -2] Name or service not known")
        self.specs.append(spec)
        runs = self.app.state.runs
        ctx = runs.context_for_token(spec.bridge.run_token, int(spec.run_id))
        session_id = spec.resume_session_id or f"session-{spec.run_id}"
        yield SessionEvent(session_id=session_id)
        said, context = [], 1000
        for i, line in enumerate(spec.prompt.splitlines()):
            line = line.strip()
            if line.startswith("TOOL "):
                _, name, *rest = line.split(" ", 2)
                args = json.loads(rest[0]) if rest else {}
                yield ToolUseEvent(id=f"t{i}", name=name, input=args)
                result = await runs.execute_tool(ctx, name, args)
                yield ToolResultEvent(tool_use_id=f"t{i}", content=result.text, is_error=result.is_error)
                said.append(result.text)
            elif line.startswith("SAY "):
                said.append(line[4:])
            elif line.startswith("CONTEXT "):
                context = int(line.split()[1])
            elif line == "HOLD":
                while not self.released:
                    await asyncio.sleep(0.01)
            elif line.startswith("LIMITS "):  # "LIMITS name used resets_in [ALL]": ALL = the report is complete
                _, name, used, resets_in, *rest = line.split()
                yield LimitsEvent(windows=[LimitWindow(name=name, utilization=float(used), resets_at=int(time.time()) + int(resets_in))], complete=rest == ["ALL"])
            elif line.startswith("COMPACT "):
                yield CompactionEvent(tokens_before=int(line.split()[1]))
        text = " | ".join(said) or "ok"
        yield AssistantTextEvent(text=text)
        yield UsageEvent(context_tokens=context)
        yield ResultEvent(session_id=session_id, text=text, num_turns=1, input_tokens=10, output_tokens=5, cost_usd=0.001, context_tokens=context)

    async def complete(self, spec: CompletionSpec) -> CompletionResult:
        self.completions.append(spec)
        return CompletionResult(text=f"SUMMARY #{len(self.completions)}", input_tokens=100, output_tokens=10, cost_usd=0.0001)

    async def cancel(self, run_id: int) -> None:
        pass

    async def limits(self, provider, credentials) -> LimitsEvent:
        self.limit_calls.append((provider, credentials))
        return LimitsEvent(windows=[w for w in self.limit_windows if w.name.startswith("codex_") == (provider == "codex")], complete=provider == "codex")

    async def notify_run(self, run_id: int, text: str, urgent: bool = False) -> bool:
        self.notices.append((run_id, text))
        return True

    async def start_job(self, spec: JobSpec) -> JobState:
        self.job_specs.append(spec)
        self.jobs[spec.id] = JobState(running=True, log_path=f"/jobs/{spec.id}.log")
        return self.jobs[spec.id]

    async def job_status(self, ids: list[str]) -> dict[str, JobState]:
        if not self.reachable:
            raise RunnerError("Runner unreachable")
        return {i: self.jobs[i] for i in ids if i in self.jobs}

    async def job_ports(self, ids: list[str]) -> dict[str, list]:
        if not self.reachable:
            raise RunnerError("Runner unreachable")
        return {i: self.ports[i] for i in ids if i in self.ports}

    async def stop_job(self, runner_id: str) -> bool:
        job = self.jobs.get(runner_id)
        return bool(job and job.running)

    async def branch_head(self, credentials, remote, branch: str) -> str | None:
        return self.branches.get(branch)

    async def assemble_train(self, spec) -> TrainResult:
        self.train_specs.append(spec)
        result = TrainResult(base=spec.base or "main", head_sha=f"train-{len(self.train_specs)}")
        for branch in spec.members:
            if branch not in self.branches:
                result.missing.append(branch)
            elif spec.skip_unchanged.get(branch) == self.branches[branch]:
                result.unchanged.append(branch)
            elif branch in self.on_base:
                result.landed.append(branch)
            elif branch in self.conflicting:
                result.conflicts.append(branch)
            else:
                result.merged[branch] = self.branches[branch]
        if spec.github and result.merged:
            result.number, result.url = 100 + len(self.train_specs), f"https://github.com/{spec.github}/pull/{100 + len(self.train_specs)}"
            self.pulls[(spec.github, result.number)] = PullRequestState(
                state="open", mergeable="yes", head_sha=result.head_sha, url=result.url, checks=[PullRequestCheck(name="ci / test", state="pending", link="https://ci/run/1")]
            )
        self.train_results.append(result)
        return result

    async def commit_checks(self, credentials, repo: str, sha: str) -> list:
        if getattr(self, "checks_error", ""):
            raise RunnerError(self.checks_error)
        return list(self.commit_checks_of.get(sha, []))

    async def check_train(self, spec) -> JobState:
        self.check_specs.append(spec)
        self.jobs[spec.id] = JobState(running=True, log_path=f"/jobs/{spec.id}.log")
        return self.jobs[spec.id]

    async def land_train(self, spec) -> CheckResult:
        self.land_specs.append(spec)
        return CheckResult(ok=not self.land_error, detail=self.land_error or "pushed")

    async def pull_request(self, credentials, repo: str, number: int) -> PullRequestState:
        if (repo, number) not in self.pulls:
            raise RunnerError("no such pull request")
        return self.pulls[(repo, number)]

    async def merge_pull_request(self, credentials, repo: str, number: int, **options) -> CheckResult:
        if self.merge_error:
            return CheckResult(ok=False, detail=self.merge_error)
        self.merged.append((repo, number, options))
        self.pulls[(repo, number)] = self.pulls[(repo, number)].model_copy(update={"state": "merged"})
        return CheckResult(ok=True, detail="merge")

    async def close_pull_request(self, credentials, repo: str, number: int) -> None:
        self.closed.append(number)

    async def upstream(self, credentials, repository: str, branch: str, commit: str):
        self.upstream_calls.append((repository, branch, commit))
        if self.upstream_error:
            raise RunnerError(self.upstream_error)
        return self.upstream_state

    async def providers(self) -> list[ProviderInfo]:
        return [
            ProviderInfo(
                name="claude_code",
                label="Claude Code",
                models=[ModelInfo(id="claude-sonnet-5-5", label="Sonnet", tier="medium"), ModelInfo(id="claude-haiku-5-5", label="Haiku", tier="small")],
            ),
            ProviderInfo(
                name="codex",
                label="Codex",
                models=[ModelInfo(id="gpt-6.1-sol", label="Sol", tier="large"), ModelInfo(id="gpt-6.1-terra", label="Terra", tier="medium"), ModelInfo(id="gpt-6-luna", label="Luna", tier="small")],
            ),
        ]

    async def check_provider(self, spec):
        return CheckResult(ok=True, detail="fake")

    async def github_create_repo(self, credentials, name, owner="", private=True):
        self.github_token_used = credentials.github_token
        full = f"{owner or 'me'}/{name}"
        return {"ok": True, "detail": "", "repos": [{"full_name": full, "name": name, "clone_url": f"https://github.com/{full}.git", "private": private}]}

    async def tidy_branches(self, spec):
        self.tidy_specs = getattr(self, "tidy_specs", []) + [spec]
        return TidyResult(deleted=[b for b in getattr(self, "merged_branches", []) if b not in spec.keep])

    async def remove_files(self, spec):
        self.removed = getattr(self, "removed", []) + [spec]
        return CheckResult(ok=not getattr(self, "remove_fails", False), detail="2 folders removed" if not getattr(self, "remove_fails", False) else "Permission denied")

    async def check_github(self, credentials, repo_url):
        return CheckResult(ok=bool(credentials.github_token), detail="fake")

    async def github_repos(self, credentials):
        self.github_token_used = credentials.github_token
        if not credentials.github_token:
            return {"ok": False, "detail": "No GitHub token configured", "repos": []}
        return {"ok": True, "detail": "", "repos": [{"full_name": "acme/api", "name": "api", "clone_url": "https://github.com/acme/api.git"}]}

    async def aclose(self) -> None:
        pass


ADMIN_PASSWORD = "admin-password"


POSTGRES = bool(os.environ.get("PLAINBOARD_DATABASE"))  # the whole suite runs against PostgreSQL


def empty_database() -> None:
    """With PostgreSQL every test starts with an empty database; with SQLite each has a file of its own anyway."""
    if not POSTGRES:
        return
    from sqlalchemy import create_engine, text

    from plainboard import config

    engine = create_engine(config.get_config().sync_database_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


@pytest.fixture
def lands_on_red(monkeypatch):
    """The time after the hold: finished branches land although the base branch is red. What happens then
    (which repair is ordered, and when the manager decides) is what these tests are about."""
    from plainboard.services import landings

    monkeypatch.setattr(landings, "RED_HOLD_SECONDS", 0)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAINBOARD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PLAINBOARD_UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("PLAINBOARD_STATIC_DIR", str(tmp_path / "static"))
    monkeypatch.setenv("PLAINBOARD_SCHEDULER_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("PLAINBOARD_JOB_WATCH_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("PLAINBOARD_LANDING_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("PLAINBOARD_LANDING_LINGER_SECONDS", "0")
    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN", "PLAINBOARD_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    from plainboard import config, main

    config.get_config.cache_clear()
    empty_database()
    monkeypatch.setattr(main, "RunnerClient", FakeRunner)
    app = main.create_app()
    FakeRunner.app = app
    with TestClient(app) as test_client:
        test_client.app_ref = app
        # A fresh installation has no users: its first admin is who the tests work as.
        assert test_client.post("/api/auth/setup", json={"username": "admin", "name": "Admin", "password": ADMIN_PASSWORD}).status_code == 201
        yield test_client
    config.get_config.cache_clear()


def wait_for(predicate, timeout: float = 10.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not met in time")


@pytest.fixture
def project(client):
    resp = client.post("/api/projects", json={"name": "Demo Project", "description": "A test project"})
    assert resp.status_code == 201, resp.text
    return resp.json()


def agents_by_slug(client, project_id: int) -> dict:
    return {a["slug"]: a for a in client.get(f"/api/projects/{project_id}/agents").json()}


def runs_of(client, project_id: int, **params) -> list[dict]:
    return client.get(f"/api/projects/{project_id}/runs", params=params).json()


def idle(client, project_id: int) -> bool:
    return not runs_of(client, project_id, active=True)
