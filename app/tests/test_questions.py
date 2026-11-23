"""Questions beside the chat (Ctrl+Enter): a read-only turn of its own, for the person who asked only."""

import json

from conftest import agents_by_slug, runs_of
from test_users import as_admin, new_user, sign_in

from plainboard.orchestration import questions


def ask(client, pid: int, text: str, thread: str | None = None) -> tuple[int, list[dict]]:
    resp = client.post(f"/api/projects/{pid}/questions", json={"text": text, "thread": thread})
    if resp.status_code != 200:
        return resp.status_code, []
    return 200, [json.loads(line) for line in resp.text.splitlines() if line.strip()]


def test_a_question_is_answered_beside_the_chat_and_only_reads(client, project):
    pid = project["id"]
    manager = agents_by_slug(client, pid)["manager"]
    runner = client.app_ref.state.runner
    status, lines = ask(client, pid, "Where is the login?\nSAY In app/auth.py:12, see #3.")
    assert status == 200
    assert lines[0]["type"] == "thread" and lines[-1] == {"type": "done", "text": "In app/auth.py:12, see #3."}

    spec = runner.specs[-1]
    assert spec.toolset == "read" and spec.max_turns == questions.MAX_TURNS
    assert spec.model == "claude-haiku-5-5"  # the provider's small model, by default
    assert spec.workspace.path.endswith("/.ask") and spec.credentials.secrets == {}
    names = {t.name for t in spec.bridge.tools}
    assert names and names <= questions.TOOLS and "get_task" in names and "read_chat" in names
    assert not names & {"delegate", "create_tasks", "add_comment", "create_note", "message_agent", "start_job", "attach_file", "send_files", "remember"}

    # Nothing of it reaches the manager: no message in its chat, no turn of its own, and it is not busy.
    conv = client.get(f"/api/agents/{manager['id']}/conversation").json()
    assert not any("login" in m["content"] for m in conv["messages"])
    (run,) = runs_of(client, pid)
    assert run["trigger"] == "question" and run["status"] == "succeeded" and run["input_text"] == "" and run["agent_id"] == manager["id"]
    stats = client.get(f"/api/projects/{pid}/stats").json()
    assert sum(a["runs"] for a in stats["agents"]) == 1


def test_a_question_cannot_change_anything(client, project):
    pid = project["id"]
    writes = [
        'TOOL create_tasks {"tickets": [{"title": "Sneaky"}]}',
        'TOOL delegate {"agent": "manager", "title": "x", "instructions": "y"}',
        'TOOL create_note {"title": "n", "content": "c"}',
        'TOOL start_job {"command": "rm -rf /"}',
        "TOOL list_tasks {}",
    ]
    status, lines = ask(client, pid, "\n".join(writes))
    assert status == 200 and lines[-1]["type"] == "done"
    answer = lines[-1]["text"]
    assert answer.count("is not available to you") == 4
    assert client.get(f"/api/projects/{pid}/tasks").json() == [] and client.get(f"/api/projects/{pid}/notes").json() == []
    assert [line["name"] for line in lines if line["type"] == "tool"] == ["create_tasks", "delegate", "create_note", "start_job", "list_tasks"]


def test_the_turns_token_uploads_nothing(client, project):
    runs = client.app_ref.state.runs
    from plainboard.orchestration.run_manager import RunContext

    token = RunContext(run_id=-5, project_id=project["id"], agent_id=1, token="q-token", tools=questions.TOOLS)
    runs._tokens[token.token] = token
    try:
        headers = {"authorization": "Bearer q-token"}
        upload = client.post("/api/internal/runs/-5/attachments", data={"task_number": "1"}, files={"file": ("a.txt", b"x")}, headers=headers)
        shown = client.post("/api/internal/runs/-5/chat-files", data={"attachment_ids": ""}, headers=headers)
        assert upload.status_code == 403 and shown.status_code == 403
    finally:
        runs._tokens.pop(token.token, None)


def test_a_follow_up_goes_on_in_the_same_session_for_its_person_only(client, project):
    pid = project["id"]
    runner = client.app_ref.state.runner
    _, first = ask(client, pid, "SAY one")
    thread = first[0]["id"]
    _, second = ask(client, pid, "SAY two", thread)
    assert second[-1]["text"] == "two" and runner.specs[-1].resume_session_id == f"session-{runner.specs[-2].run_id}"
    assert len(runs_of(client, pid)) == 2

    new_user(client, "max")
    max_id = next(u["id"] for u in client.get("/api/users").json() if u["username"] == "max")
    assert client.put(f"/api/projects/{pid}/members/{max_id}", json={"role": "member"}).status_code == 200
    sign_in(client, "max", "max-password")
    status, _ = ask(client, pid, "SAY three", thread)  # someone else's conversation
    assert status == 404
    as_admin(client)
    assert client.delete(f"/api/projects/{pid}/questions/{thread}").status_code == 204
    assert ask(client, pid, "SAY four", thread)[0] == 404  # closed


def test_who_may_chat_with_the_manager_may_ask(client, project):
    pid = project["id"]
    new_user(client, "vic")
    vic = next(u["id"] for u in client.get("/api/users").json() if u["username"] == "vic")
    assert client.put(f"/api/projects/{pid}/members/{vic}", json={"role": "viewer"}).status_code == 200
    sign_in(client, "vic", "vic-password")
    assert ask(client, pid, "SAY hi")[0] == 403
    as_admin(client)
    assert client.put(f"/api/projects/{pid}/members/{vic}", json={"role": "member"}).status_code == 200
    sign_in(client, "vic", "vic-password")
    status, lines = ask(client, pid, "SAY hi")
    assert status == 200 and lines[-1]["text"] == "hi"
    as_admin(client)
    other = client.post("/api/projects", json={"name": "Other"}).json()
    sign_in(client, "vic", "vic-password")
    assert ask(client, other["id"], "SAY hi")[0] in (403, 404)


def test_the_project_chooses_the_model_of_its_questions(client, project):
    pid = project["id"]
    runner = client.app_ref.state.runner
    resp = client.patch(f"/api/projects/{pid}", json={"ask_provider": "codex", "ask_model": "gpt-6-luna", "ask_effort": "low"})
    assert resp.status_code == 200 and resp.json()["ask_model"] == "gpt-6-luna"
    ask(client, pid, "SAY hi")
    spec = runner.specs[-1]
    assert (spec.provider, spec.model, spec.effort) == ("codex", "gpt-6-luna", "low")
    client.patch(f"/api/projects/{pid}", json={"ask_provider": "", "ask_model": "", "ask_effort": ""})
    ask(client, pid, "SAY hi")
    assert (runner.specs[-1].provider, runner.specs[-1].model) == ("claude_code", "claude-haiku-5-5")
