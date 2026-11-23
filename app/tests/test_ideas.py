"""A project's ideas (#91): collected, sorted by a model into topics, and planned into tickets by a person."""

import json
import re

import pytest
from conftest import agents_by_slug, runs_of, wait_for
from plainboard_protocol import CompletionResult
from test_transfer import bring_in, export
from test_users import new_user, sign_in

from plainboard.orchestration import ideas as idea_orchestration
from plainboard.services import ideas as idea_service


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(idea_orchestration, "SORT_DELAY", 0.0)


def answer(client, *texts, fail: bool = False) -> list:
    """The model's answers, in turn; the specs it was asked with are returned."""
    runner = client.app_ref.state.runner
    asked: list = []
    queue = list(texts)

    async def complete(spec):
        runner.database_free("complete")
        asked.append(spec)
        if fail:
            raise RuntimeError("model down")
        text = queue.pop(0) if queue else ""
        return CompletionResult(text=text(spec) if callable(text) else text, input_tokens=50, output_tokens=20, cost_usd=0.001)

    runner.complete = complete
    return asked


def ideas_of(client, pid: int, **params) -> dict:
    resp = client.get(f"/api/projects/{pid}/ideas", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def add(client, pid: int, text: str, **more) -> dict:
    resp = client.post(f"/api/projects/{pid}/ideas", json={"text": text, **more})
    assert resp.status_code == 201, resp.text
    return resp.json()


def settled(client, pid: int) -> dict:
    wait_for(lambda: not (data := ideas_of(client, pid))["sorting"] and all(i["sort_state"] != "pending" for i in data["ideas"]))
    return ideas_of(client, pid)


def test_ideas_are_sorted_into_topics_with_similar_ones_and_duplicates(client, project, monkeypatch):
    pid = project["id"]

    def first_answer(spec) -> str:
        (idea_id,) = re.findall(r"^#(\d+) ", spec.prompt, re.M)
        return json.dumps({"ideas": [{"id": int(idea_id), "topic": "Payment", "hint": ""}], "topics": [{"name": "Payment", "hint": "1 idea about payment"}]})

    asked = answer(client, first_answer)
    first = add(client, pid, "Pay by card")
    data = settled(client, pid)
    assert len(asked) == 1 and data["ideas"][0]["id"] == first["id"] and data["ideas"][0]["sort_state"] == "sorted"
    (topic,) = data["topics"]
    assert topic["name"] == "Payment" and topic["hint"] == "1 idea about payment" and topic["color"] == 0

    monkeypatch.setattr(idea_orchestration, "SORT_DELAY", 0.5)  # ideas that come quickly are sorted together

    def second_answer(spec) -> str:
        second_id, third_id = (int(n) for n in re.findall(r"^#(\d+) ", spec.prompt.split("Sort these:")[1], re.M))
        reply = {
            "ideas": [
                {"id": second_id, "topic": "payment", "similar": [first["id"], 999], "hint": "similar to #%d" % first["id"]},
                {"id": third_id, "topic": "Payment", "duplicate_of": first["id"]},
            ],
            "topics": [{"name": "Payment", "hint": "3 ideas about payment"}],
        }
        return "Here you go:\n```json\n" + json.dumps(reply)[:-1] + ",}\n```"

    asked = answer(client, second_answer)
    second, third = add(client, pid, "PayPal too", title="PayPal"), add(client, pid, "Accept credit cards")
    data = settled(client, pid)
    assert len(asked) == 1  # both together, in one call
    assert asked[0].system_prompt == idea_service.SORT_SYSTEM and "Pay by card" in asked[0].prompt and "PayPal too" in asked[0].prompt
    assert asked[0].model == "claude-haiku-5-5"  # the model of the project's questions
    by_id = {i["id"]: i for i in data["ideas"]}
    assert len(data["topics"]) == 1 and {i["topic_id"] for i in by_id.values()} == {topic["id"]}
    assert by_id[second["id"]]["similar"] == [first["id"]] and by_id[second["id"]]["hint"] == "similar to #%d" % first["id"]
    assert by_id[third["id"]]["duplicate_of"] == first["id"]
    assert sorted(by_id[first["id"]]["similar"]) == sorted([second["id"], third["id"]])
    assert data["topics"][0]["hint"] == "3 ideas about payment"

    runs = [r for r in runs_of(client, pid) if r["trigger"] == "ideas"]
    assert len(runs) == 2 and all(r["agent_id"] == agents_by_slug(client, pid)["manager"]["id"] and r["input_text"] == "" for r in runs)

    # Sorting starts nothing: no ticket, no turn of anyone.
    assert client.get(f"/api/projects/{pid}/tasks").json() == []
    assert all(r["trigger"] == "ideas" for r in runs_of(client, pid))


def test_a_broken_answer_or_a_failing_model_leaves_the_idea_unsorted(client, project):
    pid = project["id"]
    answer(client, "Sorry, I cannot do that.")
    idea = add(client, pid, "Dark mode")
    (data,) = settled(client, pid)["ideas"]
    assert data["id"] == idea["id"] and data["sort_state"] == "failed" and data["topic_id"] is None

    answer(client, json.dumps({"ideas": [{"id": "nonsense"}], "topics": "x"}))
    client.post(f"/api/projects/{pid}/ideas/sort")
    (data,) = settled(client, pid)["ideas"]
    assert data["sort_state"] == "failed" and data["topic_id"] is None

    answer(client, fail=True)
    client.post(f"/api/projects/{pid}/ideas/sort")
    (data,) = settled(client, pid)["ideas"]
    assert data["sort_state"] == "failed"
    assert [r["status"] for r in runs_of(client, pid) if r["trigger"] == "ideas"].count("failed") == 2  # the second answered, wrongly


def test_parse_json_finds_the_object_in_what_a_model_writes():
    assert idea_service.parse_json('```json\n{"a": [1, 2,],}\n```') == {"a": [1, 2]}
    assert idea_service.parse_json('Sure! {"a": 1} Hope that helps.') == {"a": 1}
    for bad in ("", "[1, 2]", "{not json", None):
        with pytest.raises(ValueError):
            idea_service.parse_json(bad)


def test_viewers_read_the_ideas_and_members_change_them(client, project):
    pid = project["id"]
    answer(client)
    viewer, member = new_user(client, "vera"), new_user(client, "max")
    for user, role in ((viewer, "viewer"), (member, "member")):
        assert client.put(f"/api/projects/{pid}/members/{user['id']}", json={"role": role}).status_code == 200
    idea = add(client, pid, "Offline mode")

    sign_in(client, "vera", "vera-password")
    assert [i["id"] for i in ideas_of(client, pid)["ideas"]] == [idea["id"]]
    assert client.post(f"/api/projects/{pid}/ideas", json={"text": "x"}).status_code == 403
    assert client.patch(f"/api/projects/{pid}/ideas/{idea['id']}", json={"votes": 3}).status_code == 403
    assert client.delete(f"/api/projects/{pid}/ideas/{idea['id']}").status_code == 403
    assert client.post(f"/api/projects/{pid}/ideas/{idea['id']}/ticket", json={}).status_code == 403
    assert client.post(f"/api/projects/{pid}/ideas/plans", json={}).status_code == 403
    assert client.post(f"/api/projects/{pid}/ideas/sort").status_code == 403

    sign_in(client, "max", "max-password")
    mine = add(client, pid, "Push notifications")
    assert mine["author"] == "Max"
    topic = client.post(f"/api/projects/{pid}/ideas/topics", json={"name": "Mobile"}).json()
    moved = client.patch(f"/api/projects/{pid}/ideas/{mine['id']}", json={"topic_id": topic["id"], "x": 10, "y": 20, "votes": 2}).json()
    assert (moved["topic_id"], moved["x"], moved["votes"], moved["sort_state"]) == (topic["id"], 10, 2, "sorted")
    assert client.patch(f"/api/projects/{pid}/ideas/topics/{topic['id']}", json={"name": "Phone"}).json()["name"] == "Phone"
    assert client.delete(f"/api/projects/{pid}/ideas/{idea['id']}").status_code == 204
    assert [i["id"] for i in ideas_of(client, pid, closed=True)["ideas"]] == [mine["id"]]


def test_planning_proposes_tickets_and_only_the_confirmed_proposal_creates_them(client, project):
    pid = project["id"]
    assert client.patch(f"/api/projects/{pid}", json={"customer_languages": ["de", "en"]}).status_code == 200
    client.post(f"/api/projects/{pid}/tasks", json={"title": "Existing login"})
    answer(client)
    a, b, c, d = (add(client, pid, text) for text in ("Pay by card", "PayPal", "Pay by card please", "Login with Google"))
    settled(client, pid)
    proposal = {
        "tickets": [
            {
                "title": "Payments: card and PayPal",
                "description": "Both providers.",
                "priority": "high",
                "labels": ["backend", 7],
                "idea_ids": [a["id"], b["id"], 12345],
                "customer_title": "Bezahlen mit Karte und PayPal",
                "customer_description": "Du kannst mit Karte oder PayPal zahlen.",
                "customer_texts": {"en": {"title": "Pay by card or PayPal", "description": "Pay as you like."}, "fr": {"title": "x"}},
            },
            {"title": "", "idea_ids": [d["id"]]},
        ],
        "left_out": [{"idea_id": c["id"], "reason": "duplicate of #%d" % a["id"]}],
    }
    asked = answer(client, "```\n" + json.dumps(proposal) + "\n```")
    plan = client.post(f"/api/projects/{pid}/ideas/plans", json={}).json()
    assert plan["status"] == "running" and sorted(plan["idea_ids"]) == sorted(i["id"] for i in (a, b, c, d))
    wait_for(lambda: client.get(f"/api/projects/{pid}/ideas/plans/{plan['id']}").json()["status"] != "running")
    plan = client.get(f"/api/projects/{pid}/ideas/plans/{plan['id']}").json()
    assert plan["status"] == "ready", plan
    assert "Existing login" in asked[0].prompt and asked[0].system_prompt == idea_service.PLAN_SYSTEM
    assert asked[0].model == agents_by_slug(client, pid)["manager"]["model"]
    (ticket,) = plan["tickets"]
    assert ticket["idea_ids"] == [a["id"], b["id"]] and ticket["labels"] == ["backend"] and ticket["priority"] == "high"
    assert list(ticket["customer_texts"]) == ["en"]
    assert {o["idea_id"]: o["reason"] for o in plan["left_out"]} == {c["id"]: "duplicate of #%d" % a["id"], d["id"]: ""}
    # Nothing is created by the proposal itself.
    assert [t["title"] for t in client.get(f"/api/projects/{pid}/tasks").json()] == ["Existing login"]

    # The person keeps the first ticket, adds one and leaves out the duplicate.
    body = {
        "status": "todo",
        "plan_id": plan["id"],
        "tickets": [ticket, {"title": "Google login", "idea_ids": [d["id"]]}],
        "dismiss": [{"idea_id": c["id"], "reason": "duplicate"}],
    }
    applied = client.post(f"/api/projects/{pid}/ideas/plans/apply", json=body)
    assert applied.status_code == 201, applied.text
    tickets = {t["title"]: t for t in applied.json()["tickets"]}
    assert set(tickets) == {"Payments: card and PayPal", "Google login"} and all(t["status"] == "todo" for t in tickets.values())
    paid = tickets["Payments: card and PayPal"]
    assert paid["customer_title"] == "Bezahlen mit Karte und PayPal"
    assert client.get(f"/api/projects/{pid}/ideas/plans/{plan['id']}").status_code == 404

    # The ideas left the board of ideas but are kept, with their ticket.
    assert ideas_of(client, pid)["ideas"] == []
    kept = {i["id"]: i for i in ideas_of(client, pid, closed=True)["ideas"]}
    assert (kept[a["id"]]["status"], kept[a["id"]]["task_id"], kept[a["id"]]["task_number"]) == ("taken", paid["id"], paid["number"])
    assert kept[b["id"]]["task_id"] == paid["id"] and kept[d["id"]]["task_number"] == tickets["Google login"]["number"]
    assert (kept[c["id"]]["status"], kept[c["id"]]["closed_reason"], kept[c["id"]]["task_id"]) == ("dismissed", "duplicate", None)
    assert all(kept[i]["text"] for i in kept)

    # The manager is told on its next turn; no turn starts for it.
    from plainboard.db import sessionmaker
    from plainboard.orchestration import inbox

    manager_id = agents_by_slug(client, pid)["manager"]["id"]

    async def notes() -> list[str]:
        async with sessionmaker()() as session:
            return await inbox.consume(session, manager_id)

    (note,) = [n for n in client.portal.call(notes) if "ideas" in n]
    assert f"#{paid['number']}" in note and "To do" in note

    # The same proposal cannot be applied twice.
    again = client.post(f"/api/projects/{pid}/ideas/plans/apply", json=body)
    assert again.status_code == 409 and len(client.get(f"/api/projects/{pid}/tasks").json()) == 3


def test_a_broken_proposal_fails_and_creates_nothing(client, project):
    pid = project["id"]
    answer(client)
    add(client, pid, "Dark mode")
    settled(client, pid)
    answer(client, '{"tickets": "none"}')
    plan = client.post(f"/api/projects/{pid}/ideas/plans", json={}).json()
    wait_for(lambda: client.get(f"/api/projects/{pid}/ideas/plans/{plan['id']}").json()["status"] != "running")
    plan = client.get(f"/api/projects/{pid}/ideas/plans/{plan['id']}").json()
    assert plan["status"] == "failed" and plan["error"]
    assert client.get(f"/api/projects/{pid}/tasks").json() == [] and len(ideas_of(client, pid)["ideas"]) == 1
    assert client.post(f"/api/projects/{pid}/ideas/plans", json={"idea_ids": []}).status_code == 400


def test_one_idea_becomes_a_ticket_and_the_project_moves_with_its_ideas(client, project):
    pid = project["id"]
    answer(client)
    topic = client.post(f"/api/projects/{pid}/ideas/topics", json={"name": "Look"}).json()
    one = add(client, pid, "Dark mode\nfor the night", topic_id=topic["id"])
    two = add(client, pid, "Bigger fonts", topic_id=topic["id"])
    resp = client.post(f"/api/projects/{pid}/ideas/{one['id']}/ticket", json={"status": "backlog"})
    assert resp.status_code == 201, resp.text
    (ticket,) = resp.json()["tickets"]
    assert ticket["title"] == "Dark mode" and ticket["description"] == "Dark mode\nfor the night" and ticket["status"] == "backlog"
    assert [i["id"] for i in ideas_of(client, pid)["ideas"]] == [two["id"]]

    moved = bring_in(client, export(client, pid))
    assert moved.status_code == 201, moved.text
    other = moved.json()["project"]["id"]
    data = ideas_of(client, other, closed=True)
    assert [t["name"] for t in data["topics"]] == ["Look"]
    by_text = {i["text"]: i for i in data["ideas"]}
    assert by_text["Bigger fonts"]["topic_id"] == data["topics"][0]["id"]
    taken = by_text["Dark mode\nfor the night"]
    assert taken["status"] == "taken" and taken["task_number"] == ticket["number"] and taken["task_id"] != ticket["id"]
