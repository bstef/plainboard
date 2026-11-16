"""Pull requests on GitHub, read and merged with the gh CLI. The app asks for this on a timer, so
that no agent has to spend a turn (or sit in one) waiting for checks."""

from __future__ import annotations

import json
from typing import Any

from plainboard_protocol import CheckResult, Credentials, PullRequestCheck, PullRequestState

from .workspace import WorkspaceError, git_env, run_cmd

FIELDS = "state,mergeable,headRefOid,url,title,statusCheckRollup"
PASSED = {"SUCCESS", "NEUTRAL", "SKIPPED"}
MERGE_METHODS = ("squash", "merge", "rebase")  # tried in this order: a repository may allow only some


def check_state(item: dict[str, Any]) -> PullRequestCheck:
    """One entry of GitHub's status roll-up: a check run (Actions and apps) or a commit status."""
    if item.get("__typename") == "StatusContext":
        state = (item.get("state") or "").upper()
        result = "pass" if state == "SUCCESS" else "pending" if state in ("PENDING", "EXPECTED") else "fail"
        return PullRequestCheck(name=item.get("context") or "status", state=result, link=item.get("targetUrl") or "")
    name = " / ".join(part for part in (item.get("workflowName"), item.get("name")) if part) or "check"
    if (item.get("status") or "").upper() != "COMPLETED":
        result = "pending"
    else:
        result = "pass" if (item.get("conclusion") or "").upper() in PASSED else "fail"
    return PullRequestCheck(name=name, state=result, link=item.get("detailsUrl") or "")


def parse_state(data: dict[str, Any]) -> PullRequestState:
    mergeable = {"MERGEABLE": "yes", "CONFLICTING": "conflict"}.get((data.get("mergeable") or "").upper(), "unknown")
    # A re-run adds a second entry for the same check: the last one counts.
    checks = {c.name: c for c in (check_state(item) for item in data.get("statusCheckRollup") or [])}
    return PullRequestState(
        state={"MERGED": "merged", "CLOSED": "closed"}.get((data.get("state") or "").upper(), "open"),
        mergeable=mergeable,  # type: ignore[arg-type]
        head_sha=data.get("headRefOid") or "",
        url=data.get("url") or "",
        title=data.get("title") or "",
        checks=list(checks.values()),
    )


async def pull_request(credentials: Credentials, repo: str, number: int) -> PullRequestState:
    out = await run_cmd(["gh", "pr", "view", str(number), "--repo", repo, "--json", FIELDS], cwd=None, env=git_env(credentials), timeout=60)
    return parse_state(json.loads(out))


async def merge(credentials: Credentials, repo: str, number: int) -> CheckResult:
    env = git_env(credentials)
    error = ""
    for method in MERGE_METHODS:
        try:
            await run_cmd(["gh", "pr", "merge", str(number), "--repo", repo, f"--{method}", "--delete-branch"], cwd=None, env=env, timeout=120)
            return CheckResult(ok=True, detail=method)
        except WorkspaceError as exc:
            error = str(exc)
            if "not allowed" not in error.lower() and "not enabled" not in error.lower():
                break  # not about the merge method: another one would fail the same way
    return CheckResult(ok=False, detail=error[-600:])
