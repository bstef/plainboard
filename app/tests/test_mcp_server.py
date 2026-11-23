"""PlainBoard's own MCP server (api/mcp_server.py) with personal tokens (services/personal_tokens.py)."""

import asyncio
import json
from datetime import timedelta

import pytest
from conftest import ADMIN_PASSWORD, agents_by_slug, wait_for
from test_users import new_user, sign_in


def mcp(client, token: str | None, method: str, params: dict | None = None, **headers):
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, headers={"Accept": "application/json, text/event-stream", **headers})


def call(client, token: str, tool: str, **arguments) -> tuple[bool, str]:
    """(ok, text) of a tool call."""
    resp = mcp(client, token, "tools/call", {"name": tool, "arguments": arguments})
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    return not result["isError"], result["content"][0]["text"]


def make_token(client, **body) -> str:
    resp = client.post("/api/tokens", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


def db(fn):
    from plainboard.db import sessionmaker

    async def run():
        async with sessionmaker()() as session:
            return await fn(session)

    return asyncio.run(run())


def test_a_token_is_shown_once_and_only_a_valid_one_gets_in(client, project):
    created = client.post("/api/tokens", json={"label": "Laptop", "expires_in_days": 30}).json()
    secret = created["token"]
    assert secret.startswith("pbp_") and created["expires_at"] and created["hint"] == secret[-4:]
    listed = client.get("/api/tokens").json()
    assert [t["label"] for t in listed] == ["Laptop"] and listed[0]["token"] is None
    assert call(client, secret, "list_projects") == (True, f"{project['id']} demo-project | Demo Project | owner")
    assert client.get("/api/tokens").json()[0]["last_used_at"]

    # No token, a wrong one, the login cookie alone: no way in.
    assert mcp(client, None, "tools/list").status_code == 401
    assert mcp(client, "pbp_wrong", "tools/list").status_code == 401
    assert "WWW-Authenticate" in mcp(client, None, "tools/list").headers
    # A page of another site can't send it, whatever it holds.
    assert mcp(client, secret, "tools/list", Origin="https://evil.example").status_code == 403
    # A token makes no tokens and doesn't change its person's password.
    bearer = {"Authorization": f"Bearer {secret}"}
    client.cookies.clear()
    assert client.post("/api/tokens", json={}, headers=bearer).status_code == 403
    assert client.get("/api/tokens", headers=bearer).status_code == 403
    assert client.patch("/api/auth/me", json={"name": "X"}, headers=bearer).status_code == 403
    # ... but reads the API as its person.
    assert client.get(f"/api/projects/{project['id']}", headers=bearer).status_code == 200
    sign_in(client, "admin", ADMIN_PASSWORD)

    # Expired: not accepted.
    from plainboard.models import PersonalToken, utcnow

    async def expire(session):
        token = await session.get(PersonalToken, created["id"])
        token.expires_at = utcnow() - timedelta(minutes=1)
        await session.commit()

    db(expire)
    assert mcp(client, secret, "tools/list").status_code == 401
    # Revoked: not accepted; only one's own can be revoked.
    other = make_token(client)
    assert mcp(client, other, "tools/list").status_code == 200
    other_id = client.get("/api/tokens").json()[-1]["id"]
    new_user(client, "mia")
    sign_in(client, "mia", "mia-password")
    assert client.delete(f"/api/tokens/{other_id}").status_code == 404
    sign_in(client, "admin", ADMIN_PASSWORD)
    assert client.delete(f"/api/tokens/{other_id}").status_code == 204
    assert mcp(client, other, "tools/list").status_code == 401


def test_read_only_tokens_switched_off_people_and_the_global_switch(client, project):
    reader = make_token(client, read_only=True)
    assert call(client, reader, "board_summary", project="demo-project")[0]
    ok, text = call(client, reader, "create_ticket", project="demo-project", title="Nope")
    assert not ok and "only read" in text
    assert client.get(f"/api/projects/{project['id']}/tasks").json() == []

    member = new_user(client, "max")
    client.put(f"/api/projects/{project['id']}/members/{member['id']}", json={"role": "member"})
    sign_in(client, "max", "max-password")
    token = make_token(client)
    sign_in(client, "admin", ADMIN_PASSWORD)
    assert mcp(client, token, "tools/list").status_code == 200
    assert client.patch(f"/api/users/{member['id']}", json={"enabled": False}).status_code == 200
    assert mcp(client, token, "tools/list").status_code == 401

    assert client.put("/api/settings", json={"values": {"mcp_enabled": False}}).status_code == 200
    assert mcp(client, reader, "tools/list").status_code == 404
    client.cookies.clear()
    assert client.get("/api/projects", headers={"Authorization": f"Bearer {reader}"}).status_code == 401
    sign_in(client, "admin", ADMIN_PASSWORD)
    assert client.post("/api/tokens", json={}).status_code == 403


def test_every_tool_acts_with_the_rights_the_ui_gives(client, project):
    pid = project["id"]
    tokens = {}
    for role in ("viewer", "member", "owner", None):
        name = f"user{role or 'out'}"
        user = new_user(client, name)
        if role:
            assert client.put(f"/api/projects/{pid}/members/{user['id']}", json={"role": role}).status_code == 200
        sign_in(client, name, f"{name}-password")
        tokens[role] = make_token(client)
        sign_in(client, "admin", ADMIN_PASSWORD)
    tokens["admin"] = make_token(client)
    ticket = client.post(f"/api/projects/{pid}/tasks", json={"title": "Existing"}).json()

    for role, token in tokens.items():
        ok, text = call(client, token, "list_projects")
        assert ("demo-project" in text) == (role is not None), role
        reads = call(client, token, "get_ticket", project="demo-project", number=ticket["number"])
        assert reads[0] == (role is not None) and ("Existing" in reads[1]) == (role is not None), role
        writes = {
            "create_ticket": dict(project="demo-project", title=f"By {role}"),
            "add_comment": dict(project="demo-project", number=ticket["number"], body=f"From {role}"),
            "create_note": dict(project="demo-project", title=f"Note of {role}"),
        }
        for tool, args in writes.items():
            ok, text = call(client, token, tool, **args)
            assert ok == (role in ("member", "owner", "admin")), (role, tool, text)
        # The same as the REST API with the same token.
        client.cookies.clear()
        rest = client.post(f"/api/projects/{pid}/tasks", json={"title": "Via REST"}, headers={"Authorization": f"Bearer {token}"}).status_code
        assert rest == {"viewer": 403, None: 404}.get(role, 201), role
        sign_in(client, "admin", ADMIN_PASSWORD)

    titles = {t["title"] for t in client.get(f"/api/projects/{pid}/tasks").json()}
    assert {"By member", "By owner", "By admin"} <= titles and "By viewer" not in titles
    # A ticket comment carries its writer's name where several people work.
    comments = client.get(f"/api/tasks/{ticket['id']}/comments").json()
    assert {c["author_name"] for c in comments} >= {"Usermember", "Userowner"}


def test_the_board_the_team_and_the_manager_through_mcp(client, project, monkeypatch):
    pid = project["id"]
    token = make_token(client)
    ok, text = call(client, token, "create_ticket", project="Demo Project", title="Login broken", description="Steps", labels=["bug"], customer_title="Login")
    assert ok and text == "Created #1 [backlog] Login broken"
    assert call(client, token, "update_ticket", project="demo-project", number=1, status="todo", priority="high")[1] == "Updated #1 [todo] (high) Login broken"
    assert call(client, token, "list_tickets", project=str(pid), label="bug")[1].startswith("#1 [todo] (high) Login broken")
    assert call(client, token, "list_tickets", project=str(pid), query="nothing like it")[1] == "No tickets."
    assert call(client, token, "add_comment", project="demo-project", number=1, body="Seen on Safari")[0]
    ok, text = call(client, token, "upload_attachment", project="demo-project", number=1, filename="log.txt", text="Error 500")
    attachment_id = int(text.split()[1])
    assert call(client, token, "get_attachment", attachment_id=attachment_id)[1].endswith("Error 500")
    ticket = call(client, token, "get_ticket", project="demo-project", number=1)[1]
    assert "Steps" in ticket and "Seen on Safari" in ticket and f"/api/attachments/{attachment_id}" in ticket and "Customer title: Login" in ticket
    assert not call(client, token, "get_ticket", project="demo-project", number=99)[0]

    # A decision the manager asked for, answered from outside.
    from plainboard.models import Task
    from plainboard.schemas import DecisionOption
    from plainboard.services import decisions as decision_service
    from sqlalchemy import select

    async def ask(session):
        task = await session.scalar(select(Task).where(Task.project_id == pid, Task.number == 1))
        return (await decision_service.create(session, task, "Which fix?", options=[DecisionOption(label="Quick"), DecisionOption(label="Proper")])).id

    decision_id = db(ask)
    assert call(client, token, "list_decisions", project="demo-project")[1] == f"Decision {decision_id} on #1: Which fix?\n  1. Quick\n  2. Proper"
    assert call(client, token, "answer_decision", decision_id=decision_id, option=2)[0]
    assert call(client, token, "list_decisions", project="demo-project")[1] == "No open decisions."

    # The manager: a message, then its answer.
    assert "manager" in call(client, token, "list_agents", project="demo-project")[1]
    ok, text = call(client, token, "chat", project="demo-project", message="SAY Hello from the manager")
    run_id = int(text.split("run ")[1].split(",")[0])
    answer = wait_for(lambda: (r := call(client, token, "wait_for_reply", run_id=run_id, wait_seconds=5)[1]) and "still" not in r and r)
    assert answer == "Hello from the manager"
    assert "Hello from the manager" in call(client, token, "read_chat", project="demo-project")[1]
    assert call(client, token, "in_flight", project="demo-project")[0]
    assert call(client, token, "list_runs", project="demo-project")[1].startswith(f"run {run_id}")
    assert call(client, token, "list_assignments", project="demo-project")[1] == "No assignments."
    assert call(client, token, "create_note", project="demo-project", title="Conventions", content="Use German")[1] == "Note 1 created."
    assert call(client, token, "get_note", project="demo-project", note_id=1)[1] == "# Conventions\n\nUse German"
    assert call(client, token, "list_schedules", project="demo-project")[0]
    assert call(client, token, "mentions")[1] == "No mentions."
    assert call(client, token, "usage_limits", project="demo-project")[0]

    # Every call is in the audit, with its project.
    from plainboard.models import McpCall

    async def audit(session):
        return list(await session.scalars(select(McpCall).order_by(McpCall.id)))

    calls = db(audit)
    assert calls[0].tool == "create_ticket" and calls[0].project_id == pid and calls[0].ok
    assert any(c.tool == "get_ticket" and not c.ok and "#99" in c.error for c in calls)
    assert len(json.dumps(mcp(client, token, "tools/list").json())) < 16000  # the tools' cost for every client


def test_a_token_that_runs_in_circles_is_slowed_down(client, project, monkeypatch):
    client.app_ref.state.mcp.limit.rate = 3
    token = make_token(client)
    assert [mcp(client, token, "tools/list").status_code for _ in range(4)] == [200, 200, 200, 429]


@pytest.mark.parametrize("path", ["/api/tokens", "/api/tokens/{token_id}"])
def test_tokens_are_each_persons_own(path):
    from plainboard.services.access import Need, rule_for

    assert rule_for(path).read == Need.USER and rule_for(path).write == Need.USER
