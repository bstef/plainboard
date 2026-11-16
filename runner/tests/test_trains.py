"""Trains with real git: several branches merged into one, checked once, pushed to the base branch."""

import asyncio
import subprocess
from pathlib import Path

import pytest
from plainboard_protocol import Credentials, RepoSpec, TrainCheckSpec, TrainLandSpec, TrainSpec

from plainboard_runner import jobs, trains

GIT = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "init.defaultBranch=main", "-c", "protocol.file.allow=always"]


def git(cwd: Path, *args: str) -> str:
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(work: Path, name: str, content: str, message: str) -> None:
    (work / name).write_text(content)
    git(work, "add", name)
    git(work, "commit", "-q", "-m", message)


@pytest.fixture
def remote(tmp_path, monkeypatch) -> Path:
    """A remote with main and four branches: a and b are independent, c conflicts with a, d builds on b."""
    monkeypatch.setattr(trains, "trains_root", lambda: tmp_path / "trains")
    monkeypatch.setattr(jobs, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(jobs, "_jobs", {})
    bare, work = tmp_path / "remote.git", tmp_path / "work"
    bare.mkdir()
    git(bare, "init", "-q", "--bare", "-b", "main")
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "remote", "add", "origin", str(bare))
    commit(work, "a.txt", "one\n", "start")
    git(work, "push", "-q", "origin", "main")
    for branch, name, content, start in (("feat-a", "a.txt", "from a\n", "main"), ("feat-b", "b.txt", "b\n", "main"), ("feat-c", "a.txt", "from c\n", "main"), ("feat-d", "d.txt", "d\n", "feat-b")):
        git(work, "checkout", "-q", "-b", branch, start)
        commit(work, name, content, f"work on {branch}")
        git(work, "push", "-q", "origin", branch)
    git(work, "checkout", "-q", "main")
    return bare


def spec(remote: Path, members: list[str], **extra) -> TrainSpec:
    return TrainSpec(credentials=Credentials(), key="p1-r1", remote=RepoSpec(name="api", url=str(remote)), branch="plainboard/train-1", members=members, **extra)


def heads(remote: Path) -> dict[str, str]:
    lines = git(remote, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads").splitlines()
    return dict(line.split(" ") for line in lines)


async def test_a_train_holds_every_branch_that_merges_and_leaves_out_the_rest(remote):
    result = await trains.assemble(spec(remote, ["feat-a", "feat-b", "feat-c", "gone"]))
    assert result.base == "main"
    assert set(result.merged) == {"feat-a", "feat-b"} and result.merged["feat-a"] == heads(remote)["feat-a"]
    assert result.conflicts == ["feat-c"] and result.missing == ["gone"]  # a conflict keeps one branch back, not the train
    clone = trains.folder("p1-r1")
    assert (clone / "a.txt").read_text() == "from a\n" and (clone / "b.txt").exists()
    assert git(clone, "rev-parse", "HEAD") == result.head_sha
    assert "plainboard/train-1" not in heads(remote)  # checked by a command: nothing is pushed until it has passed


async def test_a_branch_built_on_another_rides_along_and_landed_or_unchanged_ones_are_left_out(remote, tmp_path):
    result = await trains.assemble(spec(remote, ["feat-d", "feat-b"]))
    assert set(result.merged) == {"feat-d", "feat-b"} and not result.landed  # b came with d: still part of this train

    work = tmp_path / "work"
    git(work, "merge", "-q", "--no-ff", "-m", "land b", "feat-b")
    git(work, "push", "-q", "origin", "main")
    result = await trains.assemble(spec(remote, ["feat-b", "feat-a"], skip_unchanged={"feat-a": heads(remote)["feat-a"]}))
    assert result.landed == ["feat-b"] and result.unchanged == ["feat-a"] and not result.merged  # nothing to check


async def test_a_checked_train_is_pushed_to_the_base_branch_and_its_branches_are_removed(remote):
    result = await trains.assemble(spec(remote, ["feat-a", "feat-b"]))
    state = await trains.start_check(TrainCheckSpec(credentials=Credentials(), key="p1-r1", id="ab12", command="test -f b.txt && grep -q 'from a' a.txt && echo checked"))
    for _ in range(200):
        state = jobs.status(["ab12"])["ab12"]
        if not state.running:
            break
        await asyncio.sleep(0.02)
    assert state.exit_code == 0 and "checked" in state.tail  # the command sees all branches together

    land = TrainLandSpec(credentials=Credentials(), key="p1-r1", remote=RepoSpec(name="api", url=str(remote)), base="main", head_sha=result.head_sha, delete_branches={name: result.merged[name] for name in ("feat-a", "feat-b")} | {"feat-c": "0" * 40})
    landed = await trains.land(land)
    assert landed.ok and heads(remote)["main"] == result.head_sha
    assert set(heads(remote)) == {"main", "feat-c", "feat-d"}  # c is not at the commit named: it stays


async def test_a_train_does_not_land_on_a_base_branch_that_moved(remote, tmp_path):
    result = await trains.assemble(spec(remote, ["feat-a"]))
    work = tmp_path / "work"
    commit(work, "other.txt", "x\n", "someone pushed to main")
    git(work, "push", "-q", "origin", "main")
    moved = heads(remote)["main"]
    landed = await trains.land(TrainLandSpec(credentials=Credentials(), key="p1-r1", remote=RepoSpec(name="api", url=str(remote)), base="main", head_sha=result.head_sha))
    assert not landed.ok and heads(remote)["main"] == moved  # never forced: it has to be assembled and checked again
    again = await trains.assemble(spec(remote, ["feat-a"]))
    assert again.merged and (trains.folder("p1-r1") / "other.txt").exists()


async def test_the_head_of_a_branch_on_the_remote(remote):
    access = RepoSpec(name="api", url=str(remote))
    assert await trains.branch_head(Credentials(), access, "feat-a") == heads(remote)["feat-a"]
    assert await trains.branch_head(Credentials(), access, "nope") is None


async def test_a_branch_that_changes_the_checks_rides_alone(remote, tmp_path):
    work = tmp_path / "work"
    git(work, "checkout", "-q", "-b", "ci-x", "main")
    (work / ".github").mkdir()
    commit(work, ".github/ci.yml", "jobs: {}\n", "change the checks")
    git(work, "push", "-q", "origin", "ci-x")
    git(work, "checkout", "-q", "main")
    # Later in line it is left for the next train, so a mistake in it cannot fail the others ...
    result = await trains.assemble(spec(remote, ["feat-a", "ci-x", "feat-b"], alone_paths=[".github/"]))
    assert list(result.merged) == ["feat-a", "feat-b"] and result.held == ["ci-x"]
    assert not (trains.folder("p1-r1") / ".github").exists()
    # ... and first in line it is the whole train.
    result = await trains.assemble(spec(remote, ["ci-x", "feat-a", "feat-b"], alone_paths=[".github/"]))
    assert list(result.merged) == ["ci-x"] and result.held == ["feat-a", "feat-b"]
    assert (trains.folder("p1-r1") / ".github" / "ci.yml").exists() and not (trains.folder("p1-r1") / "b.txt").exists()
    result = await trains.assemble(spec(remote, ["feat-a", "ci-x", "feat-b"]))
    assert list(result.merged) == ["feat-a", "ci-x", "feat-b"] and result.held == []
