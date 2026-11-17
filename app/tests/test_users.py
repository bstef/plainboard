import pytest
from conftest import ADMIN_PASSWORD, agents_by_slug
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


def sign_in(client, username: str, password: str) -> None:
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"username": username, "password": password}).status_code == 200


def as_admin(client) -> None:
    sign_in(client, "admin", ADMIN_PASSWORD)


def new_user(client, username: str, **more) -> dict:
    resp = client.post("/api/users", json={"username": username, "name": username.title(), "password": f"{username}-password", **more})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_a_new_installation_gets_its_first_admin_in_the_browser(client, monkeypatch):
    from plainboard import config

    # The fixture did the set-up; what it looked like before is shown by a second installation's answers.
    status = client.get("/api/auth/status").json()
    assert status["setup_required"] is False and status["user"]["username"] == "admin" and status["user"]["is_admin"]
    assert client.post("/api/auth/setup", json={"username": "second", "password": "another-password"}).status_code == 409  # only once

    client.cookies.clear()
    assert client.get("/api/auth/status").json() == {"setup_required": False, "setup_password_required": False, "user": None}
    assert client.get("/api/projects").status_code == 401
    assert client.post("/api/auth/login", json={"username": "admin", "password": "wrong-password"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "nobody", "password": ADMIN_PASSWORD}).status_code == 401
    as_admin(client)
    assert client.get("/api/projects").status_code == 200
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/projects").status_code == 401

    # On a server the setup password keeps strangers from claiming a fresh installation.
    monkeypatch.setenv("PLAINBOARD_PASSWORD", "pw")
    config.get_config.cache_clear()
    assert client.get("/api/auth/status").json()["setup_password_required"] is True


def test_the_setup_password_guards_a_fresh_installation(tmp_path, monkeypatch):
    from conftest import FakeRunner

    from plainboard import config, main

    for name, value in (("DATA_DIR", tmp_path / "data"), ("UPLOADS_DIR", tmp_path / "uploads"), ("STATIC_DIR", tmp_path / "static"), ("PASSWORD", "pw")):
        monkeypatch.setenv(f"PLAINBOARD_{name}", str(value))
    config.get_config.cache_clear()
    monkeypatch.setattr(main, "RunnerClient", FakeRunner)
    try:
        with TestClient(main.create_app()) as client:
            assert client.get("/api/auth/status").json() == {"setup_required": True, "setup_password_required": True, "user": None}
            body = {"username": "Hanna", "name": "Hanna", "password": "long-enough"}
            assert client.post("/api/auth/setup", json=body).status_code == 403
            assert client.post("/api/auth/setup", json={**body, "setup_password": "nope"}).status_code == 403
            assert client.post("/api/auth/setup", json={**body, "password": "short", "setup_password": "pw"}).status_code == 400
            created = client.post("/api/auth/setup", json={**body, "setup_password": "pw"})
            assert created.status_code == 201 and created.json()["username"] == "hanna" and created.json()["is_admin"]
            assert client.get("/api/projects").status_code == 200  # signed in at once
    finally:
        config.get_config.cache_clear()


def test_a_project_is_seen_by_its_members_only_and_a_role_limits_what_they_do(client, project):
    pid = project["id"]
    alone = client.post("/api/projects", json={"name": "Mine alone"}).json()
    viewer, member, owner = new_user(client, "vera"), new_user(client, "max"), new_user(client, "olga")
    for user, role in ((viewer, "viewer"), (member, "member"), (owner, "owner")):
        assert client.put(f"/api/projects/{pid}/members/{user['id']}", json={"role": role}).status_code == 200
    assert [(m["username"], m["role"]) for m in client.get(f"/api/projects/{pid}/members").json()] == [("admin", "owner"), ("max", "member"), ("olga", "owner"), ("vera", "viewer")]
    manager = agents_by_slug(client, pid)["manager"]
    task = client.post(f"/api/projects/{pid}/tasks", json={"title": "Build it"}).json()
    other_task = client.post(f"/api/projects/{alone['id']}/tasks", json={"title": "Private"}).json()

    # A viewer reads the project and changes nothing. The other project does not exist for them.
    sign_in(client, "vera", "vera-password")
    assert [(p["name"], p["role"]) for p in client.get("/api/projects").json()] == [(project["name"], "viewer")]
    assert client.get(f"/api/projects/{pid}/tasks").status_code == 200 and client.get(f"/api/agents/{manager['id']}/conversation").status_code == 200
    assert client.post(f"/api/projects/{pid}/tasks", json={"title": "x"}).status_code == 403
    assert client.post(f"/api/agents/{manager['id']}/messages", json={"text": "hi"}).status_code == 403
    assert client.patch(f"/api/tasks/{task['id']}", json={"title": "x"}).status_code == 403
    for path in (f"/api/projects/{alone['id']}", f"/api/projects/{alone['id']}/tasks", f"/api/tasks/{other_task['id']}"):
        assert client.get(path).status_code == 404, path
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/projects/{alone['id']}/ws"):
            pass
    with client.websocket_connect(f"/api/projects/{pid}/ws"):
        pass
    # Nothing about the installation either.
    for path in ("/api/users", "/api/secrets", "/api/access-keys", "/api/inspect/schema", "/api/github/repos"):
        assert client.get(path).status_code == 403, path
    assert client.put("/api/settings", json={"values": {"max_parallel_runs": 9}}).status_code == 403
    assert client.post("/api/projects", json={"name": "Not allowed"}).status_code == 403
    assert client.get("/api/settings").status_code == 200 and [u["username"] for u in client.get("/api/users/directory").json()] == ["admin", "max", "olga", "vera"]

    # A member works in it, but the project's settings, secrets and members are the owners'.
    sign_in(client, "max", "max-password")
    assert client.post(f"/api/projects/{pid}/tasks", json={"title": "From Max"}).status_code == 201
    assert client.post(f"/api/agents/{manager['id']}/messages", json={"text": "SAY hi"}).status_code == 202
    assert client.patch(f"/api/projects/{pid}", json={"description": "x"}).status_code == 403
    assert client.get(f"/api/projects/{pid}/secrets").status_code == 403
    assert client.put(f"/api/projects/{pid}/members/{member['id']}", json={"role": "owner"}).status_code == 403
    assert client.delete(f"/api/projects/{pid}").status_code == 403

    # Several people work here: what one writes carries the name, for the agents and for each other.
    chat = client.get(f"/api/agents/{manager['id']}/conversation").json()
    assert any(m["content"] == "[Max] SAY hi" for m in chat["messages"]) or any(r["input_text"] == "[Max] SAY hi" for r in client.get(f"/api/projects/{pid}/runs").json())
    assert client.post(f"/api/tasks/{task['id']}/comments", json={"body": "Looks good"}).json()["author_name"] == "Max"

    # An owner does all of that in this project, and nothing in another one.
    sign_in(client, "olga", "olga-password")
    assert client.patch(f"/api/projects/{pid}", json={"description": "Shared"}).json()["role"] == "owner"
    assert client.get(f"/api/projects/{pid}/secrets").status_code == 200
    assert client.delete(f"/api/projects/{pid}/members/{viewer['id']}").status_code == 200
    assert client.patch(f"/api/projects/{alone['id']}", json={"description": "x"}).status_code == 404

    # No longer a member: the project is gone for her.
    sign_in(client, "vera", "vera-password")
    assert client.get("/api/projects").json() == [] and client.get(f"/api/projects/{pid}/tasks").status_code == 404


def test_admins_manage_users_and_one_admin_always_remains(client, project):
    user = new_user(client, "max")
    assert client.post("/api/users", json={"username": "Max", "password": "max-password"}).status_code == 400  # taken, whatever the case
    assert client.post("/api/users", json={"username": "a b", "password": "max-password"}).status_code == 400
    admin = next(u for u in client.get("/api/users").json() if u["username"] == "admin")
    assert client.patch(f"/api/users/{admin['id']}", json={"is_admin": False}).status_code == 400
    assert client.delete(f"/api/users/{admin['id']}").status_code == 400

    # Someone allowed to create projects owns what they create; nobody else sees it, except the admins.
    assert client.patch(f"/api/users/{user['id']}", json={"may_create_projects": True}).json()["may_create_projects"] is True
    sign_in(client, "max", "max-password")
    own = client.post("/api/projects", json={"name": "Max' project"}).json()
    assert own["role"] == "owner" and [p["id"] for p in client.get("/api/projects").json()] == [own["id"]]

    # One's own password: the old one is needed, and other browsers are signed out.
    other_browser = dict(client.cookies)
    assert client.patch("/api/auth/me", json={"current_password": "nope", "new_password": "a-new-password"}).status_code == 403
    assert client.patch("/api/auth/me", json={"name": "Maximilian", "current_password": "max-password", "new_password": "a-new-password"}).json()["name"] == "Maximilian"
    assert client.get("/api/projects").status_code == 200  # this browser stays signed in
    client.cookies.clear()
    client.cookies.update(other_browser)
    assert client.get("/api/projects").status_code == 401
    sign_in(client, "max", "a-new-password")

    # Switched off by an admin: signed out at once, and no sign-in.
    as_admin(client)
    assert {p["id"] for p in client.get("/api/projects").json()} == {project["id"], own["id"]}
    assert client.patch(f"/api/users/{user['id']}", json={"enabled": False}).status_code == 200
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"username": "max", "password": "a-new-password"}).status_code == 401


def test_every_route_has_a_rule(client):
    """A route without a rule would be for admins only: fine for safety, but nobody decided it."""
    from plainboard.api import browser, preview, ws
    from plainboard.main import API_MODULES
    from plainboard.services.access import Need, rule_for

    paths = [route.path for module in (*API_MODULES, ws, browser, preview) for route in module.router.routes if route.path.startswith("/api/")]
    assert len(paths) > 100 and "/api/projects/{project_id}/tasks" in paths and "/api/projects/{project_id}/ws" in paths
    assert [path for path in paths if rule_for(path) is None] == []
    # The beginning of a path decides, at a "/": a longer rule wins over a shorter one.
    assert rule_for("/api/projects").write == Need.CREATOR and rule_for("/api/projects/{project_id}").write == Need.OWNER
    assert rule_for("/api/projects/{project_id}/tasks/archive").write == Need.MEMBER
    assert rule_for("/api/secrets").read == Need.ADMIN and rule_for("/api/secrets/{secret_id}").read == Need.OWNER
    assert rule_for("/api/auth/login").read == Need.PUBLIC and rule_for("/api/auth/me").read == Need.USER
    assert rule_for("/api/authors") is None
