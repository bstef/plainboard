"""External runners update themselves: when, and what the managers hear."""

from __future__ import annotations

import io
import json
import tarfile
import time

import pytest
from conftest import FakeRunner, agents_by_slug, runs_of, wait_for

from plainboard.orchestration.runner_client import UpdateRefused
from plainboard.services import runner_update
from plainboard.services.runner_update import DRAIN_JOBS_SECONDS, FAILED, IDLE, RESTART, STAGE, WAIT, step

OWN, OLD = "a" * 40, "b" * 40


def test_a_runner_updates_only_when_its_version_differs_and_only_once_it_is_free():
    assert step(OWN, OWN, False, False, None, 0, 0, 100) == IDLE
    assert step("", OLD, False, False, None, 0, 0, 100) == IDLE  # the app does not know its own version
    assert step(OWN, "", False, False, None, 0, 0, 100) == IDLE
    assert step(OWN, OLD, False, False, None, 3, 2, 100) == STAGE  # staging touches nothing that runs
    assert step(OWN, OLD, False, False, 100, 1, 0, 100 + 10 * DRAIN_JOBS_SECONDS) == WAIT  # running turns end first
    assert step(OWN, OLD, False, False, 100, 0, 2, 100 + DRAIN_JOBS_SECONDS - 1) == WAIT  # jobs get their time
    assert step(OWN, OLD, False, False, 100, 0, 2, 100 + DRAIN_JOBS_SECONDS) == RESTART  # then it goes ahead
    assert step(OWN, OLD, False, False, 100, 0, 0, 101) == RESTART
    assert step(OWN, OLD, True, False, None, 0, 0, 100) == FAILED
    assert step(OWN, OLD, True, True, None, 0, 0, 100) == IDLE  # no loop: told, then left alone
    assert step(OWN, OLD, False, True, None, 0, 0, 100) == IDLE


def test_the_bundle_holds_the_runners_code_of_this_version_with_its_checksum():
    bundle = runner_update.bundle(OWN)
    assert bundle is not None and bundle.commit == OWN
    import hashlib

    assert hashlib.sha256(bundle.data).hexdigest() == bundle.sha256
    names = tarfile.open(fileobj=io.BytesIO(bundle.data), mode="r:gz").getnames()
    assert {"COMMIT", "plainboard_runner/selfupdate.py", "plainboard_protocol/tunnel.py", "meta/Dockerfile", "meta/launcher.py", "meta/runner.toml"} <= set(names)
    assert not [n for n in names if "__pycache__" in n]


class Updatee(FakeRunner):
    def __init__(self, refuse: str = "") -> None:
        super().__init__("tunnel")
        self.refuse = refuse
        self.staged: list[str] = []
        self.restarted: list[str] = []

    async def active_runs(self) -> set[int]:
        return set()

    async def stage_update(self, data: bytes, sha256: str, commit: str, timeout: float = 90) -> None:
        if self.refuse:
            raise UpdateRefused(self.refuse)
        self.staged.append(commit)

    async def restart_update(self, commit: str) -> None:
        self.restarted.append(commit)


def _setup(client, project, monkeypatch, runner):
    from plainboard.services import version as version_service

    monkeypatch.setattr(version_service, "running_commit", lambda: OWN)
    hub = client.app_ref.state.runs.runners
    hub.make_client = lambda _rid: runner
    created = client.post("/api/runners", json={"name": "build-server"}).json()
    rid = created["runner"]["id"]
    assert client.put(f"/api/projects/{project['id']}/runner", json={"runner_id": rid}).status_code == 200
    runs = client.app_ref.state.runs

    async def no_turn(*_args) -> None:
        pass

    monkeypatch.setattr(runs, "request_wake", no_turn)
    return created, rid, runs


def _notes(client, agent_id: int) -> list[str]:
    from plainboard.db import sessionmaker
    from plainboard.models import InboxItem
    from plainboard.services.runners import MOVED_NOTE
    from sqlalchemy import select

    async def read() -> list[str]:
        async with sessionmaker()() as session:
            return list(await session.scalars(select(InboxItem.text).where(InboxItem.agent_id == agent_id, InboxItem.text != MOVED_NOTE)))

    return client.portal.call(read)


def test_a_runner_with_another_version_takes_the_update_runs_idle_and_restarts_once_free(client, project, monkeypatch):
    runner = Updatee()
    created, rid, runs = _setup(client, project, monkeypatch, runner)
    manager = agents_by_slug(client, project["id"])["manager"]
    with client.websocket_connect("/api/runners/connect", headers={"authorization": f"Bearer {created['token']}"}) as ws:
        ws.send_text(json.dumps({"t": "hello", "version": OLD, "protocol": 1, "instance": "i1"}))
        wait_for(lambda: runs.runners.connected(rid))
        client.portal.call(runs.updates.look)
        assert runner.staged == [OWN] and not runs.runners.accepts(project["id"])
        assert next(r for r in client.get("/api/runners").json() if r["id"] == rid)["updating"]

        # Drained: a new turn waits in the queue, nothing reaches the runner.
        assert client.post(f"/api/agents/{manager['id']}/messages", json={"text": "SAY hello"}).status_code == 202
        time.sleep(0.3)
        assert [r["status"] for r in runs_of(client, project["id"])] == ["queued"] and runner.specs == []

        client.portal.call(runs.updates.look)  # nothing runs: it restarts
        assert runner.restarted == [OWN] and _notes(client, manager["id"]) == []


def test_a_runner_that_cant_take_the_update_is_left_alone_and_the_manager_hears_it_once(client, project, monkeypatch):
    runner = Updatee(refuse="the new version needs a new image (its Dockerfile changed)")
    created, rid, runs = _setup(client, project, monkeypatch, runner)
    manager = agents_by_slug(client, project["id"])["manager"]
    with client.websocket_connect("/api/runners/connect", headers={"authorization": f"Bearer {created['token']}"}) as ws:
        ws.send_text(json.dumps({"t": "hello", "version": OLD, "protocol": 1, "instance": "i1"}))
        wait_for(lambda: runs.runners.connected(rid))
        client.portal.call(runs.updates.look)
        client.portal.call(runs.updates.look)
        assert runs.runners.accepts(project["id"]) and runner.restarted == []
    notes = _notes(client, manager["id"])
    assert len(notes) == 1 and "docker compose pull" in notes[0] and "Dockerfile" in notes[0]


@pytest.mark.parametrize("reported", [True, False])
def test_an_update_that_did_not_come_up_is_told_once_and_not_tried_again(client, project, monkeypatch, reported):
    runner = Updatee()
    created, rid, runs = _setup(client, project, monkeypatch, runner)
    manager = agents_by_slug(client, project["id"])["manager"]
    update = {"failed": {"commit": OWN, "reason": "ended before it reached PlainBoard (exit code 1)", "at": time.time()}} if reported else {}
    if not reported:
        runs.updates._restarted[rid] = OWN  # it was asked to restart into OWN and came back with OLD
    with client.websocket_connect("/api/runners/connect", headers={"authorization": f"Bearer {created['token']}"}) as ws:
        ws.send_text(json.dumps({"t": "hello", "version": OLD, "protocol": 1, "instance": "i2", "update": update}))
        wait_for(lambda: runs.runners.connected(rid))
        client.portal.call(runs.updates.look)
        client.portal.call(runs.updates.look)
        assert runner.staged == [] and runs.runners.accepts(project["id"])
    notes = _notes(client, manager["id"])
    assert len(notes) == 1 and "could not update itself" in notes[0]
