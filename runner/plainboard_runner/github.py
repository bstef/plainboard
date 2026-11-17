"""GitHub pull requests through the gh CLI: used where a train of branches is checked by GitHub's
own checks. Everything else about trains is plain git (trains.py) and works with any host."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from plainboard_protocol import CheckResult, Credentials, PullRequestCheck, PullRequestState, UpstreamState

from .workspace import WorkspaceError, git_env, run_cmd

FIELDS = "state,mergeable,headRefOid,headRefName,url,title,statusCheckRollup"
PASSED = {"SUCCESS", "NEUTRAL", "SKIPPED"}
MERGE_METHODS = ("squash", "merge", "rebase")  # tried in this order: a repository may allow only some
PULL_URL = re.compile(r"https://\S+/pull/(\d+)")


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
        branch=data.get("headRefName") or "",
        url=data.get("url") or "",
        title=data.get("title") or "",
        checks=list(checks.values()),
    )


async def pull_request(credentials: Credentials, repo: str, number: int) -> PullRequestState:
    out = await run_cmd(["gh", "pr", "view", str(number), "--repo", repo, "--json", FIELDS], cwd=None, env=git_env(credentials), timeout=60)
    return parse_state(json.loads(out))


async def open_pull_request(credentials: Credentials, repo: str, base: str, head: str, title: str, body: str) -> tuple[int, str]:
    """The open pull request of a branch, created if there is none: its number and URL."""
    env = git_env(credentials)
    found = json.loads(await run_cmd(["gh", "pr", "list", "--repo", repo, "--head", head, "--state", "open", "--json", "number,url"], cwd=None, env=env, timeout=60) or "[]")
    if found:
        return found[0]["number"], found[0]["url"]
    out = await run_cmd(["gh", "pr", "create", "--repo", repo, "--base", base, "--head", head, "--title", title, "--body", body], cwd=None, env=env, timeout=120)
    created = PULL_URL.search(out)
    if not created:
        raise WorkspaceError(f"Could not open the pull request: {out[-300:]}")
    return int(created.group(1)), created.group(0)


async def merge(credentials: Credentials, repo: str, number: int, methods: tuple[str, ...] | list[str] = MERGE_METHODS) -> CheckResult:
    env = git_env(credentials)
    error = ""
    for method in methods or MERGE_METHODS:
        try:
            await run_cmd(["gh", "pr", "merge", str(number), "--repo", repo, f"--{method}", "--delete-branch"], cwd=None, env=env, timeout=120)
            return CheckResult(ok=True, detail=method)
        except WorkspaceError as exc:
            error = str(exc)
            if "not allowed" not in error.lower() and "not enabled" not in error.lower():
                break  # not about the merge method: another one would fail the same way
    return CheckResult(ok=False, detail=error[-600:])


async def commit_checks(credentials: Credentials, repo: str, sha: str) -> list[PullRequestCheck]:
    """The check runs of a commit (what a push to a branch started), the latest per name. For a base
    branch that got its branches before the checks."""
    out = await run_cmd(
        ["gh", "api", f"repos/{repo}/commits/{sha}/check-runs?per_page=100", "--paginate", "--jq", ".check_runs[] | {name, status, conclusion, html_url}"],
        cwd=None, env=git_env(credentials), timeout=90,
    )  # fmt: skip
    return parse_check_runs([json.loads(line) for line in out.splitlines() if line.strip()])


def parse_check_runs(items: list[dict[str, Any]]) -> list[PullRequestCheck]:
    checks: dict[str, PullRequestCheck] = {}
    for item in items:
        conclusion = (item.get("conclusion") or "").upper()
        if (item.get("status") or "").upper() != "COMPLETED":
            state = "pending"
        elif conclusion in PASSED:
            state = "pass"
        else:
            state = "cancelled" if conclusion in ("CANCELLED", "STALE") else "fail"
        checks[item.get("name") or "check"] = PullRequestCheck(name=item.get("name") or "check", state=state, link=item.get("html_url") or "")  # type: ignore[arg-type]
    return list(checks.values())


API = "https://api.github.com"


async def upstream(credentials: Credentials, repository: str, branch: str, commit: str, *, transport: httpx.AsyncBaseTransport | None = None) -> UpstreamState | None:
    """How many commits ``branch`` of ``repository`` has that ``commit`` has not. None if GitHub does
    not know the commit there (a local build, a fork). Asked without a token first: a public
    repository needs none, and the user's token is not sent where it is not needed."""
    url = f"{API}/repos/{repository}/compare/{commit}...{branch}"
    async with httpx.AsyncClient(timeout=20, transport=transport) as client:
        tokens = [None, credentials.github_token] if credentials.github_token else [None]
        for token in tokens:
            headers = {"Accept": "application/vnd.github+json", "User-Agent": "plainboard"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            try:
                resp = await client.get(url, params={"per_page": 1}, headers=headers)
            except httpx.HTTPError as exc:
                raise WorkspaceError(f"GitHub is not reachable: {exc}") from exc
            if resp.status_code == 200:
                data = resp.json()
                return UpstreamState(behind=int(data.get("ahead_by") or 0), url=data.get("html_url") or "")
        if resp.status_code == 404:
            return None
        raise WorkspaceError(f"GitHub answered {resp.status_code} for {repository}")


async def close(credentials: Credentials, repo: str, number: int) -> bool:
    """Close a pull request that will not be merged, with its branch."""
    try:
        await run_cmd(["gh", "pr", "close", str(number), "--repo", repo, "--delete-branch"], cwd=None, env=git_env(credentials), timeout=60)
        return True
    except WorkspaceError:
        return False
