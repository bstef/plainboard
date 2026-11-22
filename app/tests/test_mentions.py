from conftest import agents_by_slug, idle, wait_for
from test_orchestration import chat, runner
from test_users import as_admin, new_user, sign_in


def test_names_are_found_outside_code_and_words():
    from plainboard.services.mentions import usernames

    text = "Hi @Luchs, see @otto.\n```\n@inblock\n```\nalso `@inline`, mail me@example.org, path x/@y, @a"
    assert {"luchs", "otto"} <= usernames(text)
    assert not usernames(text) & {"inblock", "inline", "example.org", "y", "a"}
    assert usernames("~~~\n@nope") == set()


def test_a_mention_reaches_only_people_with_access_and_is_read_once(client, project):
    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]
    luchs, otto = new_user(client, "luchs"), new_user(client, "otto")
    new_user(client, "eve")  # no access to the project
    assert client.put(f"/api/projects/{pid}/members/{luchs['id']}", json={"role": "member"}).status_code == 200
    assert client.put(f"/api/projects/{pid}/members/{otto['id']}", json={"role": "viewer"}).status_code == 200  # read-only: still mentioned
    people = client.get(f"/api/projects/{pid}/people").json()
    assert [p["username"] for p in people] == ["admin", "luchs", "otto"]

    # The agent writes it; the person's own chat message counts as well (a line of its own: the first gets the name).
    chat(client, manager["id"], "Hi\nSAY Thanks @Luchs and @otto, not @eve, @nobody or `@admin`.")
    wait_for(lambda: idle(client, pid))
    assert "@admin, @luchs, @otto" in runner(client).specs[-1].system_prompt  # the name only where it says more

    sign_in(client, "luchs", "luchs-password")
    mine = client.get("/api/mentions").json()
    assert mine["unread"] == 2 and {m["author"] for m in mine["items"]} == {"Admin", "Manager"}
    assert all(m["kind"] == "chat" and m["agent_id"] == manager["id"] for m in mine["items"])
    assert client.post("/api/mentions/read", json={"ids": [mine["items"][0]["id"]]}).status_code == 204
    assert client.get("/api/mentions").json()["unread"] == 1
    assert client.post("/api/mentions/read", json={}).status_code == 204
    assert client.get("/api/mentions").json()["unread"] == 0

    # A comment: who writes it is not notified of naming themselves.
    task = client.post(f"/api/projects/{pid}/tasks", json={"title": "Logo"}).json()
    assert client.post(f"/api/tasks/{task['id']}/comments", json={"body": "@admin @luchs have a look"}).status_code == 201
    assert client.get("/api/mentions").json()["unread"] == 0

    sign_in(client, "otto", "otto-password")
    assert client.get("/api/mentions").json()["unread"] == 2
    sign_in(client, "eve", "eve-password")
    assert client.get("/api/mentions").json() == {"unread": 0, "items": []}

    as_admin(client)
    mine = client.get("/api/mentions").json()
    assert mine["unread"] == 1
    (item,) = mine["items"]
    assert item["kind"] == "comment" and item["task_number"] == task["number"] and item["author"] == "Luchs" and item["project_name"] == project["name"]

    # Someone who loses access no longer sees the mentions of that project.
    assert client.delete(f"/api/projects/{pid}/members/{otto['id']}").status_code == 200
    sign_in(client, "otto", "otto-password")
    assert client.get("/api/mentions").json()["unread"] == 0
