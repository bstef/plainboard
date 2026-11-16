"""Trains: several finished branches merged into one, so that one check run covers all of them.

Plain git in a clone of the runner's own, so it works with any git host. The app's watcher decides
what goes on a train and what happens after its checks; nothing here needs an agent."""

from __future__ import annotations

import asyncio
from pathlib import Path

from plainboard_protocol import CheckResult, Credentials, JobSpec, JobState, RepoSpec, TrainCheckSpec, TrainLandSpec, TrainResult, TrainSpec

from . import jobs
from .workspace import WorkspaceError, config_args, git_env, run_cmd

_locks: dict[str, asyncio.Lock] = {}


def trains_root() -> Path:
    return Path.home() / ".plainboard" / "trains"


def folder(key: str) -> Path:
    return trains_root() / key


def _lock(key: str) -> asyncio.Lock:
    return _locks.setdefault(key, asyncio.Lock())


class Git:
    """git in the train's clone, with the repository's access."""

    def __init__(self, credentials: Credentials, remote: RepoSpec, cwd: Path | None):
        self.env = git_env(credentials)
        self.config = config_args(remote, credentials)
        self.cwd = cwd

    async def __call__(self, *args: str, timeout: float = 600) -> str:
        return await run_cmd(["git", *self.config, *args], cwd=self.cwd, env=self.env, timeout=timeout)

    async def ok(self, *args: str) -> bool:
        try:
            await self(*args)
            return True
        except WorkspaceError:
            return False


async def remote_heads(git: Git, url: str) -> dict[str, str]:
    heads = {}
    for line in (await git("ls-remote", "--heads", url, timeout=120)).splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith("refs/heads/"):
            heads[ref.removeprefix("refs/heads/")] = sha.strip()
    return heads


async def default_branch(git: Git, url: str) -> str:
    for line in (await git("ls-remote", "--symref", url, "HEAD", timeout=120)).splitlines():
        if line.startswith("ref: refs/heads/"):
            return line.split("\t")[0].removeprefix("ref: refs/heads/")
    raise WorkspaceError("Could not find the repository's default branch")


async def branch_head(credentials: Credentials, remote: RepoSpec, branch: str) -> str | None:
    """The commit a branch points to on the remote; None if it is not there."""
    return (await remote_heads(Git(credentials, remote, None), remote.url)).get(branch)


async def assemble(spec: TrainSpec) -> TrainResult:
    """Merge the member branches, in order, onto the base branch. A branch that conflicts is left
    out; the others still go. With ``spec.github`` the result is pushed as the train's branch."""
    where = folder(spec.key)
    url = spec.remote.url
    async with _lock(spec.key):
        where.mkdir(parents=True, exist_ok=True)
        git = Git(spec.credentials, spec.remote, where)
        if not (where / ".git").exists():
            await git("init", "-q")
            await git("remote", "add", "origin", url)
        else:
            await git("remote", "set-url", "origin", url)
        heads = await remote_heads(git, url)
        base = spec.base or await default_branch(git, url)
        if base not in heads:
            raise WorkspaceError(f"Base branch {base} not found on the remote")
        result = TrainResult(base=base)
        members = []
        for branch in dict.fromkeys(spec.members):
            if branch not in heads:
                result.missing.append(branch)
            elif spec.skip_unchanged.get(branch) == heads[branch]:
                result.unchanged.append(branch)
            else:
                members.append(branch)
        refs = [f"+refs/heads/{name}:refs/remotes/origin/{name}" for name in (base, *members)]
        await git("fetch", "-q", "--no-tags", "origin", *refs)
        await git.ok("merge", "--abort")  # a half-finished merge of an earlier attempt
        await git("checkout", "-q", "-f", "-B", spec.branch, f"refs/remotes/origin/{base}")
        await git("clean", "-q", "-fdx")
        base_sha = await git("rev-parse", "HEAD")
        alone = False  # the train is one branch that changes the checks themselves
        for branch in members:
            before = await git("rev-parse", "HEAD")
            ref = f"refs/remotes/origin/{branch}"
            # What the branch itself changed since it left the base branch, whatever was merged before it.
            changes_checks = bool(spec.alone_paths) and not await git.ok("diff", "--quiet", f"{base_sha}...{ref}", "--", *spec.alone_paths)
            if alone or (changes_checks and result.merged):
                result.held.append(branch)
                continue
            if not await git.ok("merge", "-q", "--no-ff", "--no-edit", "-m", f"Land {branch}", ref):
                if not await git.ok("merge", "--abort"):
                    await git("reset", "-q", "--hard", before)
                result.conflicts.append(branch)
            elif await git("rev-parse", "HEAD") != before or not await git.ok("merge-base", "--is-ancestor", ref, base_sha):
                result.merged[branch] = heads[branch]  # also a branch that an earlier member already brought along
                alone = changes_checks
            else:
                result.landed.append(branch)  # the base branch has it already
        result.head_sha = await git("rev-parse", "HEAD")
        if result.head_sha == base_sha:  # nothing new: nothing to check
            result.landed += list(result.merged)
            result.merged = {}
        elif spec.github:
            await git("push", "-q", "--force", "origin", f"HEAD:refs/heads/{spec.branch}")
        return result


async def start_check(spec: TrainCheckSpec) -> JobState:
    """The repository's check command, in the clone that holds the assembled train. It runs as a job:
    the app polls for its end like for any other."""
    where = folder(spec.key)
    if not (where / ".git").exists():
        raise WorkspaceError("The train has not been assembled on this runner")
    return await jobs.start(JobSpec(id=spec.id, command=spec.command, workspace=str(where), credentials=spec.credentials), cwd=where)


async def land(spec: TrainLandSpec) -> CheckResult:
    """Push the checked train to the base branch. Not forced: if the base branch moved meanwhile the
    push is refused, and the train has to be assembled and checked again."""
    where = folder(spec.key)
    async with _lock(spec.key):
        git = Git(spec.credentials, spec.remote, where)
        try:
            await git("push", "-q", "origin", f"{spec.head_sha}:refs/heads/{spec.base}", timeout=300)
        except WorkspaceError as exc:
            return CheckResult(ok=False, detail=str(exc)[-600:])
        await _remove(git, spec.delete_branches)
        return CheckResult(ok=True, detail="pushed")


async def _remove(git: Git, branches: dict[str, str]) -> None:
    """Remove landed branches from the remote. Only while a branch still points to the commit that
    landed: commits pushed to it since are not lost."""
    for branch, sha in branches.items():
        await git.ok("push", "-q", f"--force-with-lease=refs/heads/{branch}:{sha}", "origin", f":refs/heads/{branch}")


async def delete_branches(credentials: Credentials, remote: RepoSpec, key: str, branches: dict[str, str]) -> None:
    where = folder(key)
    if (where / ".git").exists():
        await _remove(Git(credentials, remote, where), branches)
