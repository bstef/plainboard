import hashlib
import subprocess
from datetime import UTC, datetime, timedelta

from plainboard_runner import snapshots


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def make_repo(path):
    path.mkdir(parents=True)
    git(path, "init", "-q")
    (path / ".gitignore").write_text("*.log\n")
    (path / "a.txt").write_text("one\n")
    (path / "b.txt").write_text("two\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "init")
    return path


def state(repo):
    return (
        git(repo, "status", "--porcelain", "--untracked-files=all"),
        hashlib.sha256((repo / ".git" / "index").read_bytes()).hexdigest(),
        git(repo, "stash", "list"),
        git(repo, "for-each-ref", "refs/heads", "refs/stash"),
        (repo / "a.txt").read_text(),
    )


def at(minutes):
    return datetime(2026, 11, 23, 12, 0, tzinfo=UTC) + timedelta(minutes=minutes)


def test_snapshot_leaves_working_tree_index_and_stash_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshots, "FILE_CAP", 1000)
    repo = make_repo(tmp_path / "ws" / "repo")
    (repo / "a.txt").write_text("changed\n")
    (repo / "b.txt").unlink()
    (repo / "c.txt").write_text("staged\n")
    git(repo, "add", "c.txt")
    (repo / "new.txt").write_text("untracked\n")
    (repo / "debug.log").write_text("ignored\n")
    (repo / "big.bin").write_bytes(b"x" * 2000)
    git(repo, "stash", "list")
    before = state(repo)

    commit = snapshots.take(repo, "dev", at(0))

    assert commit and state(repo) == before
    files = set(git(repo, "ls-tree", "-r", "--name-only", commit).splitlines())
    assert files == {".gitignore", "a.txt", "c.txt", "new.txt"}
    assert git(repo, "show", f"{commit}:a.txt") == "changed"
    assert git(repo, "rev-parse", f"{commit}^") == git(repo, "rev-parse", "HEAD")
    assert [s for s, _, _ in snapshots.listed(repo, "dev")] == ["20261123-120000"]


def test_unchanged_or_clean_work_is_not_snapshotted_again(tmp_path):
    repo = make_repo(tmp_path / "repo")
    assert snapshots.take(repo, "dev", at(0)) is None  # clean
    (repo / "a.txt").write_text("changed\n")
    assert snapshots.take(repo, "dev", at(1))
    assert snapshots.take(repo, "dev", at(2)) is None  # same tree as the last one
    git(repo, "commit", "-qam", "saved")
    assert snapshots.take(repo, "dev", at(3)) is None  # committed: nothing uncommitted
    assert len(snapshots.listed(repo, "dev")) == 1


def test_retention_keeps_the_newest_and_drops_old_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshots, "KEEP", 3)
    repo = make_repo(tmp_path / "repo")
    for i in range(5):
        (repo / "a.txt").write_text(f"v{i}\n")
        snapshots.take(repo, "dev", at(i))
    assert [s for s, _, _ in snapshots.listed(repo, "dev")] == ["20261123-120400", "20261123-120300", "20261123-120200"]
    assert snapshots.prune(repo, "dev", at(4) + timedelta(days=7, seconds=-30)) == 2
    assert [s for s, _, _ in snapshots.listed(repo, "dev")] == ["20261123-120400"]


def test_restore_into_a_branch_or_stash_keeps_the_working_tree(tmp_path):
    ws = tmp_path / "dev"
    repo = make_repo(ws / "repo")
    (repo / "a.txt").write_text("lost work\n")
    (repo / "new.txt").write_text("untracked\n")
    assert snapshots.snapshot_workspace(ws) == 1
    git(repo, "checkout", "--", "a.txt")
    (repo / "new.txt").unlink()
    [(stamp, _, _)] = snapshots.listed(repo, "dev")
    assert "repo " + stamp in snapshots.describe(ws) and "M a.txt" in snapshots.describe(ws)
    before = state(repo)

    snapshots.restore(ws, "repo", stamp, branch="rescued")
    assert state(repo)[0::4] == before[0::4]
    assert git(repo, "show", "rescued:a.txt") == "lost work"
    assert git(repo, "show", "rescued:new.txt") == "untracked"

    snapshots.restore(ws, "repo", stamp, as_stash=True)
    assert (repo / "a.txt").read_text() == "one\n"
    git(repo, "stash", "apply")
    assert (repo / "a.txt").read_text() == "lost work\n" and (repo / "new.txt").read_text() == "untracked\n"


def test_snapshot_refs_are_never_pushed(tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    repo = make_repo(tmp_path / "repo")
    git(repo, "remote", "add", "origin", str(remote))
    (repo / "a.txt").write_text("changed\n")
    assert snapshots.take(repo, "dev")
    git(repo, "push", "-q", "origin", "HEAD")
    git(repo, "push", "-q", "--all", "origin")
    git(repo, "push", "-q", "--tags", "origin")
    assert git(remote, "for-each-ref", "refs/plainboard") == ""
    assert git(remote, "for-each-ref", "--format=%(refname)") == "refs/heads/main"
