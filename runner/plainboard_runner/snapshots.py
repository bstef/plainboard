"""Snapshots of an agent's uncommitted work (#72): a last resort after e.g. ``git checkout -- src``.

Before and after every turn each git repository in the agent's workspace with uncommitted changes
(tracked changes and untracked files git does not ignore) gets a commit of its working tree under
``refs/plainboard/snapshots/<agent>/<time>``, built in a temporary index: the working tree, the real
index, the stash and the branches stay untouched. These refs are never pushed (every push names its
refspec, none matches ``refs/plainboard/*``) and the branch sweep only reads ``refs/heads``. Only the
newest ``KEEP`` per agent and repository younger than ``MAX_AGE`` stay; git's gc removes the rest.
The member tool ``workspace_snapshots`` lists them and restores one into a new branch or as a stash.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

PREFIX = "refs/plainboard/snapshots"
KEEP = 20
MAX_AGE = timedelta(days=7)
FILE_CAP = 20 * 1024 * 1024  # a bigger untracked file is left out
UNTRACKED_CAP = 200 * 1024 * 1024  # all untracked files of one snapshot together
GIT_TIMEOUT = 60
STAMP = "%Y%m%d-%H%M%S"
IDENTITY = {
    "GIT_AUTHOR_NAME": "PlainBoard",
    "GIT_AUTHOR_EMAIL": "snapshots@plainboard.local",
    "GIT_COMMITTER_NAME": "PlainBoard",
    "GIT_COMMITTER_EMAIL": "snapshots@plainboard.local",
}


class SnapshotError(RuntimeError):
    pass


def _git(repo: Path, *args: str, env: dict[str, str] | None = None, input: str | None = None, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", **(env or {})},
        input=input,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
    )
    if check and proc.returncode != 0:
        raise SnapshotError(f"git {args[0]} failed: {proc.stderr.strip()[-300:]}")
    return proc.stdout.strip() if proc.returncode == 0 else ""


def agent_of(workspace: Path) -> str:
    """The agent's part of the ref name: its workspace folder (one per agent)."""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", workspace.name).strip("-") or "agent"


def repos(workspace: Path) -> list[Path]:
    """The workspace itself, when it is a repository, and its sub folders that are."""
    found = [workspace] if (workspace / ".git").exists() else []
    try:
        found += sorted(p for p in workspace.iterdir() if p.is_dir() and not p.is_symlink() and (p / ".git").exists())
    except OSError:
        pass
    return found


def _untracked(repo: Path, env: dict[str, str]) -> list[str]:
    """Untracked files git does not ignore, within the caps (no embedded repositories)."""
    picked: list[str] = []
    total = 0
    for name in _git(repo, "ls-files", "-z", "--others", "--exclude-standard", env=env).split("\0"):
        if not name or name.endswith("/"):
            continue
        try:
            size = (repo / name).lstat().st_size
        except OSError:
            continue
        if size > FILE_CAP or total + size > UNTRACKED_CAP:
            continue
        total += size
        picked.append(name)
    return picked


def _tree(repo: Path) -> str:
    """The tree of the working tree as ``git add -A`` would stage it, in a temporary index."""
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir"))
    fd, tmp = tempfile.mkstemp(prefix="plainboard-snapshot-", suffix=".index", dir=git_dir)
    os.close(fd)
    try:
        if (git_dir / "index").exists():
            shutil.copyfile(git_dir / "index", tmp)
        else:
            os.unlink(tmp)  # git creates a fresh one
        env = {"GIT_INDEX_FILE": tmp}
        _git(repo, "add", "-u", env=env)
        if names := _untracked(repo, env):
            _git(repo, "--literal-pathspecs", "add", "--pathspec-from-file=-", "--pathspec-file-nul", env=env, input="\0".join(names))
        return _git(repo, "write-tree", env=env)
    finally:
        Path(tmp).unlink(missing_ok=True)


def listed(repo: Path, agent: str) -> list[tuple[str, str, datetime]]:
    """(id, commit, time) of the agent's snapshots in the repository, newest first."""
    out = _git(repo, "for-each-ref", "--sort=-refname", "--format=%(refname:lstrip=4) %(objectname)", f"{PREFIX}/{agent}/", check=False)
    found = []
    for line in out.splitlines():
        stamp, _, commit = line.partition(" ")
        try:
            found.append((stamp, commit, datetime.strptime(stamp, STAMP).replace(tzinfo=UTC)))
        except ValueError:
            continue
    return found


def take(repo: Path, agent: str, now: datetime | None = None) -> str | None:
    """Snapshot the repository's uncommitted work. Returns the commit, None: nothing new to keep."""
    now = now or datetime.now(UTC)
    head = _git(repo, "rev-parse", "-q", "--verify", "HEAD^{commit}", check=False)
    tree = _tree(repo)
    newest = listed(repo, agent)
    same = (head and tree == _git(repo, "rev-parse", f"{head}^{{tree}}")) or (
        newest and tree == _git(repo, "rev-parse", f"{newest[0][1]}^{{tree}}", check=False)
    )
    commit = None
    if not same:
        branch = _git(repo, "symbolic-ref", "--short", "-q", "HEAD", check=False) or "detached HEAD"
        parents = ["-p", head] if head else []
        commit = _git(repo, "commit-tree", tree, *parents, "-m", f"PlainBoard snapshot of {branch}", env=IDENTITY)
        _git(repo, "update-ref", f"{PREFIX}/{agent}/{now.strftime(STAMP)}", commit)
    prune(repo, agent, now)
    return commit


def prune(repo: Path, agent: str, now: datetime | None = None) -> int:
    """Delete all but the newest ``KEEP`` snapshots and those older than ``MAX_AGE``."""
    now = now or datetime.now(UTC)
    old = [stamp for i, (stamp, _, at) in enumerate(listed(repo, agent)) if i >= KEEP or now - at > MAX_AGE]
    if old:
        _git(repo, "update-ref", "--stdin", input="".join(f"delete {PREFIX}/{agent}/{stamp}\n" for stamp in old))
        _git(repo, "gc", "--auto", "--quiet", check=False)
    return len(old)


def snapshot_workspace(workspace: Path) -> int:
    """Snapshot every repository of the workspace; a failing one is only logged. Returns how many were taken."""
    agent = agent_of(workspace)
    taken = 0
    for repo in repos(workspace):
        try:
            taken += take(repo, agent) is not None
        except (SnapshotError, OSError, subprocess.SubprocessError) as exc:
            log.warning("No snapshot of %s: %s", repo, exc)
    return taken


def describe(workspace: Path, only: str | None = None, limit: int = 10) -> str:
    """The snapshots for the agent: per repository time, branch and changed files."""
    agent = agent_of(workspace)
    lines: list[str] = []
    for repo in repos(workspace):
        name = repo.name if repo != workspace else "."
        if only and only != name:
            continue
        for stamp, commit, at in listed(repo, agent)[:limit]:
            subject = _git(repo, "log", "-1", "--format=%s", commit, check=False).removeprefix("PlainBoard snapshot of ")
            parent = _git(repo, "rev-parse", "-q", "--verify", f"{commit}^", check=False)
            changed = (
                _git(repo, "diff", "--name-status", "--no-renames", parent, commit, check=False)
                if parent
                else "\n".join(f"A\t{n}" for n in _git(repo, "ls-tree", "-r", "--name-only", commit, check=False).splitlines())
            ).splitlines()
            files = ", ".join(c.replace("\t", " ") for c in changed[:8]) + (f" (+{len(changed) - 8} more)" if len(changed) > 8 else "")
            lines.append(f"{name} {stamp} ({at:%Y-%m-%d %H:%M} UTC, {subject}): {files}")
    return "\n".join(lines) or "No snapshots."


def restore(workspace: Path, repo_name: str, stamp: str, as_stash: bool = False, branch: str | None = None) -> str:
    """Put a snapshot back as a new branch or as a stash entry; the working tree stays as it is."""
    agent = agent_of(workspace)
    repo = workspace if repo_name in ("", ".") else workspace / repo_name
    if repo not in repos(workspace):
        raise SnapshotError(f"No repository '{repo_name}' in your workspace")
    commit = next((c for s, c, _ in listed(repo, agent) if s == stamp), None)
    if commit is None:
        raise SnapshotError(f"No snapshot {stamp} in '{repo_name}'")
    if not as_stash:
        name = branch or f"snapshot-{stamp}"
        if _git(repo, "rev-parse", "-q", "--verify", f"refs/heads/{name}", check=False):
            raise SnapshotError(f"Branch {name} exists already")
        _git(repo, "branch", name, commit)
        return f"Branch {name} holds the snapshot as one commit on top of where it was taken."
    parent = _git(repo, "rev-parse", "-q", "--verify", f"{commit}^", check=False)
    if not parent:
        raise SnapshotError("This snapshot has no base commit: restore it as a branch")
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}")
    index = _git(repo, "commit-tree", f"{parent}^{{tree}}", "-p", parent, "-m", "index", env=IDENTITY)
    stash = _git(repo, "commit-tree", tree, "-p", parent, "-p", index, "-m", f"PlainBoard snapshot {stamp}", env=IDENTITY)
    _git(repo, "stash", "store", "-m", f"PlainBoard snapshot {stamp}", stash)
    return "Stored as stash@{0}; apply it with git stash apply when you want it in your working tree."
