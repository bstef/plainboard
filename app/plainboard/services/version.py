"""Which PlainBoard is running, and whether a newer one exists.

The image knows the commit it was built from (``read_commit``, run by the Dockerfile). A watcher
without any model asks the upstream repository now and then how many commits it is ahead of that
one (orchestration/update_watcher.py); the UI shows a quiet notice. Only the commit id is sent.

Standard library only up to ``read_commit``: the image build runs it before anything is configured.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SHA = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")


def read_commit(git_dir: Path) -> str:
    """The commit a checkout is on, from its .git folder; "" if that cannot be told (no git data in
    the build context, a worktree, a broken checkout). Reads files only: the image needs no git."""
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if SHA.fullmatch(head):
            return head  # detached
        if not head.startswith("ref: "):
            return ""
        ref = head.removeprefix("ref: ").strip()
        loose = git_dir / ref
        if loose.is_file():
            sha = loose.read_text(encoding="utf-8").strip()
            return sha if SHA.fullmatch(sha) else ""
        for line in (git_dir / "packed-refs").read_text(encoding="utf-8").splitlines():
            sha, _, name = line.partition(" ")
            if name.strip() == ref and SHA.fullmatch(sha):
                return sha
    except (OSError, UnicodeDecodeError):
        pass
    return ""


if __name__ == "__main__":  # python version.py <.git folder>: used by the image build
    print(read_commit(Path(sys.argv[1])))
    raise SystemExit(0)


@dataclass
class Upstream:
    behind: int | None = None  # commits the upstream branch has that this build has not; None: not known
    url: str = ""  # where to read them
    checked_at: datetime | None = None


_upstream = Upstream()
_commit: str | None = None


def running_commit() -> str:
    """The commit this app was built from; "" if the build could not tell."""
    global _commit
    if _commit is None:
        from ..config import get_config

        config = get_config()
        _commit = config.commit.strip().lower() or read_commit_file(config.commit_file)
    return _commit


def read_commit_file(path: Path) -> str:
    try:
        sha = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return sha if SHA.fullmatch(sha) else ""


def record(behind: int | None, url: str, now: datetime | None) -> None:
    _upstream.behind, _upstream.url, _upstream.checked_at = behind, url, now


def forget() -> None:
    record(None, "", None)


def current() -> dict | None:
    """For the UI; None if this build does not know its commit."""
    commit = running_commit()
    if not commit:
        return None
    return {"commit": commit[:10], "behind": _upstream.behind, "url": _upstream.url}
