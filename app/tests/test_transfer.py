"""Moving a project to another PlainBoard as one file (services/transfer.py)."""

import io
import json
import zipfile

from conftest import agents_by_slug, idle, wait_for
from test_flow import assignments, chat, delegate, make_member, manager_of, runner


def export(client, pid: int, **body) -> bytes:
    resp = client.post(f"/api/projects/{pid}/export", json=body)
    assert resp.status_code == 200 and resp.headers["content-type"] == "application/zip", resp.text
    return resp.content


def bring_in(client, data: bytes, passphrase: str = ""):
    return client.post("/api/projects/import", files={"file": ("project.plainboard.zip", data, "application/zip")}, data={"passphrase": passphrase})


def test_a_project_moves_as_one_file_with_its_board_team_and_history(client, project):
    pid = project["id"]
    manager = manager_of(client, pid)
    make_member(client, pid, "Dev1")
    fake = runner(client)
    assert client.patch(f"/api/projects/{pid}", json={"github_token": "ghp_secret", "description": "A shop"}).status_code == 200
    assert client.post(f"/api/projects/{pid}/secrets", json={"name": "NPM_TOKEN", "value": "npm-1"}).status_code == 201
    assert client.post(f"/api/projects/{pid}/notes", json={"title": "Deploy", "content": "Tag, then push", "pinned": True}).status_code == 201
    ticket = client.post(f"/api/projects/{pid}/tasks", json={"title": "Build the cart", "description": "With totals", "status": "todo"}).json()
    assert client.post(f"/api/tasks/{ticket['id']}/comments", json={"body": "Mind the taxes"}).status_code == 201
    (picture,) = client.post(f"/api/tasks/{ticket['id']}/attachments", files={"files": ("cart.png", b"PNG-bytes", "image/png")}).json()
    chat(client, manager["id"], delegate("dev1", "Build it", "SAY cart built on branch shop/cart", ticket_number=1))
    wait_for(lambda: idle(client, pid) and assignments(client, pid) and assignments(client, pid)[0]["status"] == "done")
    # Work that is open when the file is made: it does not go on by itself on the other side.
    client.post(f"/api/projects/{pid}/tasks", json={"title": "Second", "status": "todo"})
    chat(client, manager["id"], delegate("dev1", "Go on", "HOLD\nSAY later", ticket_number=2))
    wait_for(lambda: any(a["status"] == "running" for a in assignments(client, pid)))

    data = export(client, pid, passphrase="open sesame")
    fake.released = True
    wait_for(lambda: idle(client, pid))
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        document = json.loads(archive.read("project.json"))
        assert archive.read(f"files/{document['tables']['attachments'][0]['storage_name']}") == b"PNG-bytes"
    # No credential is readable in the file: they are sealed, and the columns are empty.
    assert "ghp_secret" not in json.dumps(document) and "npm-1" not in json.dumps(document) and document["sealed"]["data"]
    assert document["tables"]["projects"][0]["github_token_enc"] is None and document["left_out"] == []

    assert bring_in(client, data, "wrong").status_code == 400
    assert bring_in(client, b"not a zip").status_code == 400
    resp = bring_in(client, data, "open sesame")
    assert resp.status_code == 201, resp.text
    moved = resp.json()
    new = moved["project"]
    assert new["id"] != pid and new["name"] == project["name"] and new["slug"] != project["slug"] and new["role"] == "owner"
    assert new["description"] == "A shop" and new["has_github_token"] and moved["missing"] == []
    assert moved["counts"]["agents"] == 2 and moved["counts"]["tasks"] == 2 and moved["counts"]["attachments"] == 1 and moved["counts"]["messages"] > 0

    team = agents_by_slug(client, new["id"])
    assert set(team) == {"manager", "dev1"} and team["manager"]["is_manager"] and team["dev1"]["id"] not in {a["id"] for a in agents_by_slug(client, pid).values()}
    tickets = {t["number"]: t for t in client.get(f"/api/projects/{new['id']}/tasks").json()}
    assert tickets[1]["title"] == "Build the cart" and tickets[1]["id"] != ticket["id"]
    detail = client.get(f"/api/tasks/{tickets[1]['id']}").json()
    assert [c["body"] for c in detail["comments"]] == ["Mind the taxes"]
    (report,) = detail["assignments"]
    assert report["status"] == "done" and "cart built" in report["result"] and report["agent_id"] == team["dev1"]["id"]
    (file,) = detail["attachments"]
    assert file["id"] != picture["id"] and client.get(f"/api/attachments/{file['id']}").content == b"PNG-bytes"
    # What was open is closed, with a word on why; nothing started by itself.
    open_one = next(a for a in assignments(client, new["id"]) if a["title"] == "Go on")
    assert open_one["status"] == "cancelled" and "moved to this PlainBoard" in open_one["result"]
    assert idle(client, new["id"]) and client.get(f"/api/projects/{new['id']}/runs").json() == []
    assert [n["title"] for n in client.get(f"/api/projects/{new['id']}/notes").json()] == ["Deploy"]
    assert [s["name"] for s in client.get(f"/api/projects/{new['id']}/secrets").json()] == ["NPM_TOKEN"]
    # The chat came along: the manager goes on from what was said, in a session of its own.
    texts = [m["content"] for m in client.get(f"/api/agents/{team['manager']['id']}/conversation").json()["messages"]]
    assert any("delegate" in text for text in texts)
    # The credentials work here: an agent of the new project gets the secret and the token.
    chat(client, team["dev1"]["id"], "SAY hello")
    wait_for(lambda: idle(client, new["id"]))
    credentials = fake.specs[-1].credentials
    assert credentials.github_token == "ghp_secret" and credentials.secrets["NPM_TOKEN"] == "npm-1"


def test_without_a_passphrase_the_credentials_stay_behind_and_are_named(client, project):
    pid = project["id"]
    assert client.patch(f"/api/projects/{pid}", json={"github_token": "ghp_secret"}).status_code == 200
    assert client.post(f"/api/projects/{pid}/secrets", json={"name": "NPM_TOKEN", "value": "npm-1"}).status_code == 201
    manager = manager_of(client, pid)
    chat(client, manager["id"], "SAY hello there")
    wait_for(lambda: idle(client, pid))

    data = export(client, pid, chats=False)
    document = json.loads(zipfile.ZipFile(io.BytesIO(data)).read("project.json"))
    assert document["sealed"] is None and "ghp_secret" not in json.dumps(document) and "messages" not in document["tables"]
    assert document["left_out"] == ["the project's GitHub token", "the secret NPM_TOKEN"]

    moved = bring_in(client, data).json()
    assert moved["missing"] == ["the project's GitHub token", "the secret NPM_TOKEN"] and not moved["project"]["has_github_token"]
    assert "messages" not in moved["counts"]
    new_manager = manager_of(client, moved["project"]["id"])
    assert client.get(f"/api/agents/{new_manager['id']}/conversation").json()["messages"] == []
    # A file with sealed credentials and no passphrase: it comes in, and says what is missing.
    sealed = bring_in(client, export(client, pid, passphrase="pw")).json()
    assert sealed["missing"] == ["everything with a credential: no passphrase was given for this file"]


def test_only_an_owner_takes_a_project_along(client):
    from plainboard.services.access import Need, rule_for

    assert rule_for("/api/projects/{project_id}/export").read == Need.OWNER and rule_for("/api/projects/{project_id}/export").write in (None, Need.OWNER)
    assert rule_for("/api/projects/import").write == Need.CREATOR
