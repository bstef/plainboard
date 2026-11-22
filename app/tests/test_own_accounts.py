"""Subscriptions people add themselves and share (Account.owner_id, services/permissions.may_use)."""

from conftest import ADMIN_PASSWORD, idle, wait_for
from test_permissions import as_friend, friend, sign_in

TOKEN = {"provider": "claude_code", "auth_mode": "oauth_token"}


def inbox(client, project_id: int) -> list[str]:
    from plainboard.db import sessionmaker
    from plainboard.models import Agent, InboxItem
    from sqlalchemy import select

    async def read() -> list[str]:
        async with sessionmaker()() as s:
            manager = await s.scalar(select(Agent.id).where(Agent.project_id == project_id, Agent.is_manager))
            return list(await s.scalars(select(InboxItem.text).where(InboxItem.agent_id == manager)))

    return client.portal.call(read)


def ids(client, project_id: int) -> set[int]:
    return set(client.get(f"/api/projects/{project_id}/accounts").json())


def test_a_person_adds_an_own_subscription_that_only_their_projects_use_and_nobody_reads_back(client, project, monkeypatch):
    installation = {a["id"] for a in client.get("/api/accounts").json()}
    friend(client)
    friend(client, "bob")
    as_friend(client)
    refused = client.post("/api/accounts", json={**TOKEN, "auth_mode": "runner_login"})
    assert refused.status_code == 400 and "token or an API key" in refused.json()["detail"]
    assert client.post("/api/accounts", json={"provider": "codex", "auth_mode": "chatgpt"}).status_code == 400
    resp = client.post("/api/accounts", json={**TOKEN, "label": "Olga's", "secret": "tok-olga"})
    assert resp.status_code == 201, resp.text
    own = resp.json()
    assert own["owner_id"] is not None and own["share"] == "private" and own["can_manage"] and own["secret_set"]
    assert "tok-olga" not in resp.text and all("tok-olga" not in str(a) for a in client.get("/api/accounts").json())
    assert client.patch(f"/api/accounts/{own['id']}", json={"auth_mode": "runner_login"}).status_code == 400
    pid = client.post("/api/projects", json={"name": "Olga's", "account_id": own["id"]}).json()["id"]
    assert ids(client, pid) == installation | {own["id"]}
    dev = client.post(f"/api/projects/{pid}/agents", json={"name": "Dev", "role": "Developer", "description": "x"}).json()
    assert client.post(f"/api/agents/{dev['id']}/messages", json={"text": "SAY hi"}).status_code == 202
    wait_for(lambda: idle(client, pid))
    assert client.app_ref.state.runner.specs[-1].credentials.claude_oauth_token == "tok-olga"

    # Not the installation's environment for an own subscription without a secret.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-installation")
    assert client.patch(f"/api/accounts/{own['id']}", json={"secret": ""}).json()["secret_from_env"] is False

    # Bob neither sees nor uses it, nor changes it; an admin's project does not run on it.
    as_friend(client, "bob")
    assert own["id"] not in {a["id"] for a in client.get("/api/accounts").json()}
    assert own["id"] not in {a["account_id"] for a in client.get("/api/limits").json()["accounts"]}
    assert client.patch(f"/api/accounts/{own['id']}", json={"label": "Mine now"}).status_code == 404
    assert client.delete(f"/api/accounts/{own['id']}").status_code == 404
    bobs = client.post("/api/projects", json={"name": "Bob's"}).json()["id"]
    assert own["id"] not in ids(client, bobs)
    assert client.post("/api/projects", json={"name": "Bob's pinned", "account_id": own["id"]}).status_code == 400
    sign_in(client, "admin", ADMIN_PASSWORD)
    assert own["id"] not in ids(client, project["id"])
    seen = next(a for a in client.get("/api/accounts").json() if a["id"] == own["id"])
    assert seen["owner_name"] and seen["can_manage"]  # an admin manages it too


def test_shared_with_people_or_everyone_and_unshared_pins_choose_again_and_managers_hear_once(client, project):
    olga = friend(client)
    bob = friend(client, "bob")
    friend(client, "eve")
    as_friend(client)
    own = client.post("/api/accounts", json={**TOKEN, "label": "Team", "secret": "tok-team"}).json()
    assert client.patch(f"/api/accounts/{own['id']}", json={"share": "people", "shared_with": [9999]}).status_code == 404
    shared = client.patch(f"/api/accounts/{own['id']}", json={"share": "people", "shared_with": [bob["id"], olga["id"]]}).json()
    assert shared["share"] == "people" and shared["shared_with"] == [bob["id"]]

    as_friend(client, "bob")
    seen = next(a for a in client.get("/api/accounts").json() if a["id"] == own["id"])
    assert not seen["can_manage"] and not seen["secret_set"] and seen["shared_with"] == [] and seen["label"] == "Team"
    assert client.patch(f"/api/accounts/{own['id']}", json={"share": "everyone"}).status_code == 403
    assert client.post(f"/api/accounts/{own['id']}/check", json={"model": "x"}).status_code == 403
    pid = client.post("/api/projects", json={"name": "Bob's", "account_id": own["id"]}).json()["id"]
    dev = client.post(f"/api/projects/{pid}/agents", json={"name": "Dev", "role": "Developer", "description": "x", "account_id": own["id"]}).json()
    assert dev["account_id"] == own["id"]
    as_friend(client, "eve")
    assert own["id"] not in {a["id"] for a in client.get("/api/accounts").json()}

    as_friend(client)
    assert client.patch(f"/api/accounts/{own['id']}", json={"share": "everyone"}).status_code == 200
    as_friend(client, "eve")
    assert own["id"] in {a["id"] for a in client.get("/api/accounts").json()}
    sign_in(client, "admin", ADMIN_PASSWORD)
    assert own["id"] in ids(client, project["id"])

    as_friend(client)
    before = len(inbox(client, pid))
    assert client.patch(f"/api/accounts/{own['id']}", json={"share": "private"}).status_code == 200
    as_friend(client, "bob")
    assert client.get(f"/api/projects/{pid}").json()["account_id"] is None
    assert next(a for a in client.get(f"/api/projects/{pid}/agents").json() if a["id"] == dev["id"])["account_id"] is None
    told = inbox(client, pid)[before:]
    assert len(told) == 1 and "'Team' is no longer shared" in told[0] and "the project, Dev" in told[0]
    assert own["id"] not in ids(client, pid)


def test_a_deleted_own_subscription_or_person_releases_what_ran_on_it(client, project):
    olga = friend(client)
    as_friend(client)
    own = client.post("/api/accounts", json={**TOKEN, "label": "Gone", "secret": "tok", "auth_mode": "api_key"}).json()
    client.patch(f"/api/accounts/{own['id']}", json={"share": "everyone"})
    later = client.post("/api/accounts", json={**TOKEN, "label": "Later", "secret": "tok2"}).json()
    sign_in(client, "admin", ADMIN_PASSWORD)
    assert client.patch(f"/api/projects/{project['id']}", json={"account_id": own["id"]}).status_code == 200
    as_friend(client)
    assert client.delete(f"/api/accounts/{own['id']}").status_code == 204
    sign_in(client, "admin", ADMIN_PASSWORD)
    assert client.get(f"/api/projects/{project['id']}").json()["account_id"] is None
    assert sum("'Gone' was deleted" in text for text in inbox(client, project["id"])) == 1
    assert client.delete(f"/api/users/{olga['id']}").status_code == 204
    assert later["id"] not in {a["id"] for a in client.get("/api/accounts").json()}
