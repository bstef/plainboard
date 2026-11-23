"""Semantic search: texts prepared for retrieval, the index kept current, hybrid ranking, isolation, the switches."""

import hashlib
import json
import math
import re
from dataclasses import replace
from datetime import datetime

import pytest
from conftest import POSTGRES, idle, wait_for
from plainboard_protocol import GitCommit
from test_flow import chat, delegate, make_member, manager_of, runner, tool_results

from plainboard.services.search import documents
from plainboard.services.search.documents import Doc

pg_only = pytest.mark.skipif(not POSTGRES, reason="semantic search needs PostgreSQL with pgvector")


# --- texts ----------------------------------------------------------------------------------------


def test_clean_keeps_the_words_and_drops_the_markup():
    text = "## Fehler\n\n**Login** schlägt fehl, siehe [Logs](https://example.com/x).\n\n```python\nrefresh_token()\n```\n> zitiert\n\n---\n<b>fett</b> ![bild](a.png)"
    assert documents.clean(text) == "Fehler\n\nLogin schlägt fehl, siehe Logs.\n\nrefresh_token()\nzitiert\n\nfett bild"


def test_long_texts_are_cut_between_paragraphs_with_an_overlap():
    paragraphs = [f"Absatz {i}: " + "wort " * 60 for i in range(12)]
    chunks = documents.split("\n\n".join(paragraphs), size=1000, overlap=400)
    assert len(chunks) > 1 and all(len(c) <= 1000 for c in chunks)
    assert all(c.startswith("Absatz") for c in chunks)  # never in the middle of a paragraph
    for before, after in zip(chunks, chunks[1:]):
        assert after.split("\n\n")[0] in before  # the next one starts with the end of the one before
    one_line = "x" * 5000  # a log without any break is still cut
    assert all(len(c) <= documents.CHUNK_CHARS for c in documents.split(one_line))


def test_each_chunk_says_what_it_is_and_its_hash_changes_only_with_its_text():
    doc = Doc(kind="ticket", source_id="1", ref="#12", title="Login via **SSO**", body="Fails after a token refresh.", at=datetime(2026, 11, 20), facts=["done", "labels: auth"], ticket_number=12)
    (chunk,) = documents.prepare(doc, "PlainBoard")
    assert chunk.header == "Ticket #12 · PlainBoard · 2026-11-20 · done · labels: auth\nLogin via SSO"
    assert chunk.body == "Fails after a token refresh."
    assert documents.prepare(doc, "PlainBoard")[0].hash == chunk.hash
    doc.facts = ["in_progress", "labels: auth"]
    assert documents.prepare(doc, "PlainBoard")[0].hash != chunk.hash
    long = Doc(kind="report", source_id="2", ref="assignment #5", title="Report", body="\n\n".join(["Satz. " * 150] * 4), at=datetime(2026, 11, 20))
    parts = documents.prepare(long, "P")
    assert len(parts) > 1 and parts[1].header.startswith("Report assignment #5 · P · 2026-11-20 · part 2 of ")
    assert documents.prepare(Doc(kind="comment", source_id="3", ref="comment on #1", title="", body="ok", at=datetime(2026, 11, 20)), "P") == []


# --- where it is absent ----------------------------------------------------------------------------


def tool_names(client) -> set[str]:
    return {t.name for t in runner(client).specs[-1].bridge.tools}


@pytest.mark.skipif(POSTGRES, reason="SQLite only")
def test_with_sqlite_there_is_no_semantic_search(client, project):
    pid = project["id"]
    assert client.get("/api/meta").json()["semantic_search"] is False
    status = client.get("/api/search/status").json()
    assert status["available"] is False and status["documents"] == 0
    assert client.portal.call(client.app_ref.state.search.tick) is False
    chat(client, manager_of(client, pid)["id"], "SAY hi")
    wait_for(lambda: idle(client, pid))
    assert "search_history" not in tool_names(client)


# --- PostgreSQL with pgvector ---------------------------------------------------------------------


class FakeEmbedder:
    """Words hashed into 256 buckets: texts that share words are close, as a real model's would be for the same topic."""

    dim, state, error, progress, rate = 256, "ready", "", None, None

    def __init__(self, name: str = "fake"):
        from plainboard.services.search.embedding import CARDS, DEFAULT

        self.name = name
        self.card = replace(CARDS[DEFAULT], name=name, hint_similarity=0.5)  # the fake's scale is not a real model's
        self.embedded: list[str] = []

    def prepare(self) -> None:
        pass

    def unload(self) -> None:
        pass

    def _vector(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for word in re.findall(r"[^\W_]{3,}", text.lower()):
            v[int(hashlib.md5(word[:6].encode()).hexdigest(), 16) % self.dim] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    async def passages(self, texts):
        self.embedded += texts
        return [self._vector(t) for t in texts]

    async def query(self, text):
        return self._vector(text)


@pytest.fixture
def embedder(monkeypatch):
    from plainboard.services.search import embedding

    fake = FakeEmbedder()
    monkeypatch.setattr(embedding, "_current", {embedding.DEFAULT: fake})
    return fake


def index(client) -> None:
    while client.portal.call(client.app_ref.state.search.tick):
        pass


def ticket(client, pid: int, title: str, description: str = "", **extra) -> dict:
    resp = client.post(f"/api/projects/{pid}/tasks", json={"title": title, "description": description, **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()


def search(client, agent_id: int, pid: int, **args) -> str:
    chat(client, agent_id, "TOOL search_history " + json.dumps(args))
    wait_for(lambda: idle(client, pid))
    return tool_results(client, agent_id)[-1]


@pg_only
def test_the_history_is_indexed_once_and_found_by_meaning_and_by_words(client, project, embedder):
    pid = project["id"]
    assert client.get("/api/meta").json()["semantic_search"] is True
    login = ticket(client, pid, "Anmeldung schlägt fehl", "Nach dem Token-Refresh fliegt man beim SSO-Login raus.", labels=["auth"])
    ticket(client, pid, "Dunkles Design für die Einstellungen", "Ein Dark-Mode-Schalter auf der Einstellungsseite.")
    assert client.post(f"/api/tasks/{login['id']}/comments", json={"body": "Der Session-Cookie wird beim Refresh nicht erneuert."}).status_code == 201
    assert client.post(f"/api/projects/{pid}/notes", json={"title": "CI-Cache", "content": "Docker-Builds in CI nutzen den buildx-Cache."}).status_code == 201
    index(client)
    status = client.get("/api/search/status").json()
    assert status["available"] and status["documents"] == status["embedded"] == 4
    assert status["by_kind"] == {"ticket": 2, "comment": 1, "note": 1}
    embedded = len(embedder.embedded)

    # Nothing changed: nothing is embedded again. A ticket moved on the board only: neither.
    index(client)
    assert client.patch(f"/api/tasks/{login['id']}", json={"position": 5}).status_code == 200
    index(client)
    assert len(embedder.embedded) == embedded
    # Its text changed: that ticket alone.
    assert client.patch(f"/api/tasks/{login['id']}", json={"description": "SSO-Login: nach dem Token-Refresh abgemeldet."}).status_code == 200
    index(client)
    assert len(embedder.embedded) == embedded + 1 and "abgemeldet" in embedder.embedded[-1]

    manager = manager_of(client, pid)
    found = search(client, manager["id"], pid, query="Anmeldung Token Refresh")
    first = found.splitlines()[0]
    assert first.startswith(f"- #{login['number']} ticket, ") and "Anmeldung schlägt fehl" in first
    assert "comment on #" in found  # the comment about it, too
    # By the start of a word (keyword part) and only what was asked for.
    only_notes = search(client, manager["id"], pid, query="buildx", kinds=["note"])
    assert only_notes.startswith("- note ") and "CI-Cache" in only_notes and "#" not in only_notes.splitlines()[0]
    assert "search_history" in tool_names(client)


@pg_only
def test_an_agent_finds_only_its_own_projects_history(client, project, embedder):
    other = client.post("/api/projects", json={"name": "Other"}).json()
    ticket(client, project["id"], "Payment provider times out", "Stripe webhook retries")
    ticket(client, other["id"], "Payment provider times out", "Stripe webhook retries in the other project")
    index(client)
    found = search(client, manager_of(client, project["id"])["id"], project["id"], query="payment webhook")
    assert len(found.splitlines()) == 2 and "other project" not in found  # one hit: its title line and snippet


@pg_only
def test_switched_off_nothing_is_indexed_and_the_agents_have_no_search(client, project, embedder):
    pid = project["id"]
    ticket(client, pid, "Something to find", "with enough words in it")
    assert client.patch(f"/api/projects/{pid}", json={"semantic_search": False}).json()["semantic_search"] is False
    index(client)
    assert client.get("/api/search/status").json()["documents"] == 0
    chat(client, manager_of(client, pid)["id"], "SAY hi")
    wait_for(lambda: idle(client, pid))
    assert "search_history" not in tool_names(client)
    # On again, but off for the whole installation: the same.
    client.patch(f"/api/projects/{pid}", json={"semantic_search": True})
    assert client.put("/api/settings", json={"values": {"semantic_search": False}}).status_code == 200
    index(client)
    assert client.get("/api/search/status").json()["documents"] == 0
    client.put("/api/settings", json={"values": {"semantic_search": True}})
    index(client)
    assert client.get("/api/search/status").json()["documents"] == 1


@pg_only
def test_commits_are_read_from_the_last_one_indexed_and_deleted_items_leave(client, project, embedder):
    from datetime import timedelta

    from plainboard.models import utcnow

    pid = project["id"]
    client.post(f"/api/projects/{pid}/repositories", json={"name": "api", "url": "https://github.com/acme/api.git"})
    fake = runner(client)
    fake.commits = [GitCommit(sha="b" * 40, author="Dev", date="2026-11-22T10:00:00+01:00", message="Fix webhook retries\n\nBackoff for Stripe.", files=["app/pay.py"], file_count=1, insertions=5, deletions=1)]
    note = client.post(f"/api/projects/{pid}/notes", json={"title": "Webhooks", "content": "Stripe sends retries for six hours."}).json()
    index(client)
    assert fake.git_log_specs[-1].since is None
    found = search(client, manager_of(client, pid)["id"], pid, query="Stripe webhook backoff", kinds=["commit"])
    assert re.match(r"- bbbbbbbbbb commit in api, 2026-11-22 \((?:.+ ago|yesterday)\); now: still on main: Fix webhook retries", found), found
    assert "Changed 1 file (+5 -1): app/pay.py" in found
    fake.gone = {"b" * 40}  # pushed over: no longer on main, and its file was removed since
    found = search(client, manager_of(client, pid)["id"], pid, query="Stripe webhook backoff", kinds=["commit"])
    assert "; now: no longer on main; 1 of its files no longer exist (app/pay.py): Fix webhook retries" in found
    fake.gone = set()

    fake.commits = [GitCommit(sha="c" * 40, message="Retry webhooks with jitter"), *fake.commits]
    later = utcnow() + timedelta(minutes=11)
    client.portal.call(client.app_ref.state.search.tick, later)
    assert fake.git_log_specs[-1].since == "b" * 40 and len(fake.git_log_specs) == 2  # from the last one read
    client.portal.call(client.app_ref.state.search.tick, later)
    assert len(fake.git_log_specs) == 2  # not with every look

    assert client.delete(f"/api/projects/{pid}/notes/{note['id']}").status_code in (200, 204)
    client.portal.call(client.app_ref.state.search.tick, utcnow() + timedelta(hours=1))
    assert client.get("/api/search/status").json()["by_kind"] == {"commit": 2}


@pg_only
def test_new_work_on_a_ticket_starts_with_what_is_very_close_in_the_history(client, project, embedder):
    pid = project["id"]
    old = ticket(client, pid, "Webhook retries of Stripe time out", "Stripe webhook retries time out after the payment")
    new = ticket(client, pid, "Stripe webhook retries time out again", "")
    index(client)
    make_member(client, pid, "Dev")
    chat(client, manager_of(client, pid)["id"], delegate("dev", "Stripe webhook retries", "Stripe webhook retries time out", ticket_number=new["number"]))
    wait_for(lambda: any("Possibly related" in s.prompt for s in runner(client).specs if s.run_id and s.prompt.count("Assignment #")))
    prompt = next(s.prompt for s in runner(client).specs if "Possibly related" in s.prompt)
    assert f"#{old['number']} ticket" in prompt and f"#{new['number']} ticket" not in prompt.split("Possibly related")[1]


@pg_only
def test_a_hit_says_how_old_it_is_and_where_its_item_stands_now(client, project, embedder):
    pid = project["id"]
    login = ticket(client, pid, "Anmeldung schlägt fehl", "Nach dem Token-Refresh fliegt man beim SSO-Login raus.")
    index(client)
    manager = manager_of(client, pid)["id"]
    assert " (today); now: ticket now backlog: Anmeldung schlägt fehl" in search(client, manager, pid, query="Anmeldung")
    # Done since it was indexed: the hit says so at once, from the ticket itself.
    assert client.patch(f"/api/tasks/{login['id']}", json={"status": "done"}).status_code == 200
    assert "now: ticket now done" in search(client, manager, pid, query="Anmeldung")


@pg_only
def test_another_model_builds_its_index_in_the_background_and_search_switches_when_it_is_complete(client, project, embedder, monkeypatch):
    from plainboard.services.search import embedding

    pid = project["id"]
    ticket(client, pid, "Anmeldung schlägt fehl", "Nach dem Token-Refresh fliegt man beim SSO-Login raus.")
    ticket(client, pid, "Dunkles Design", "Ein Dark-Mode-Schalter auf der Einstellungsseite.")
    index(client)
    other = FakeEmbedder("other")
    other.state = "absent"  # it is downloaded first
    embedding._current["multilingual-e5-base"] = other
    status = client.post("/api/search/model", json={"model": "multilingual-e5-base"}).json()
    assert status["model"]["name"] == "fake" and status["next"]["name"] == "other" and status["next_embedded"] == 0
    assert {m["name"] for m in status["models"]} >= {"multilingual-e5-small", "multilingual-e5-base"} and any(m["recommended"] for m in status["models"])
    index(client)  # the new model is not ready: search goes on with the old one
    status = client.get("/api/search/status").json()
    assert status["model"]["name"] == "fake" and status["embedded"] == 2 and status["next_embedded"] == 0
    assert "Anmeldung schlägt fehl" in search(client, manager_of(client, pid)["id"], pid, query="Anmeldung Token")
    other.state = "ready"
    index(client)
    status = client.get("/api/search/status").json()
    assert status["model"]["name"] == "other" and status["next"] is None and status["embedded"] == 2  # switched, the old vectors gone
    assert client.post("/api/search/model", json={"model": "nope"}).status_code == 400
