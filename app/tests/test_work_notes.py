"""Working notes per assignment (services/work_notes.py)."""

import json

from conftest import agents_by_slug, idle, wait_for
from test_orchestration import chat, conversation, make_dev, runner


def _running_assignment(client, pid: int, dev: dict, notes: str, *, with_turn: bool = False) -> int:
    from plainboard.db import sessionmaker
    from plainboard.models import Assignment, Run

    async def create() -> int:
        async with sessionmaker()() as s:
            a = Assignment(project_id=pid, agent_id=dev["id"], title="Review it", instructions="x", status="running", work_notes=notes)
            s.add(a)
            await s.commit()
            if with_turn:
                s.add(Run(project_id=pid, agent_id=dev["id"], trigger="assignment", input_text="x", status="running", assignment_id=a.id))
                await s.commit()
            return a.id

    return client.portal.call(create)


def _notes(client, assignment_id: int) -> str | None:
    from plainboard.db import sessionmaker
    from plainboard.models import Assignment

    async def read():
        async with sessionmaker()() as s:
            return (await s.get(Assignment, assignment_id)).work_notes

    return client.portal.call(read)


def _tool_results(client, agent_id: int) -> list[dict]:
    return [m["meta"] for m in conversation(client, agent_id)["messages"] if m["kind"] == "tool"]


def test_write_append_cap_and_gone_when_the_assignment_ends(client, project):
    pid = project["id"]
    make_dev(client, pid)
    manager = agents_by_slug(client, pid)["manager"]
    steps = [
        'TOOL write_work_notes {"text": "finding 1"}',
        'TOOL append_work_notes {"text": "finding 2"}',
        "TOOL work_notes",
        "TOOL write_work_notes " + json.dumps({"text": "x" * 30_001}),
        "SAY done",
    ]
    chat(client, manager["id"], "TOOL delegate " + json.dumps({"agent": "dev", "title": "Review", "instructions": "\n".join(steps)}))
    wait_for(lambda: idle(client, pid) and client.get(f"/api/projects/{pid}/assignments").json()[0]["status"] == "done")
    dev = agents_by_slug(client, pid)["dev"]
    results = _tool_results(client, dev["id"])
    assert results[2]["result"] == "finding 1\nfinding 2"
    assert results[3]["is_error"] and "cap is 30000" in results[3]["result"]
    (assignment,) = client.get(f"/api/projects/{pid}/assignments").json()
    assert assignment["work_notes"] is None  # discarded with the end of the assignment


def test_only_for_the_own_open_assignment(client, project):
    pid = project["id"]
    dev = make_dev(client, pid)
    other = make_dev(client, pid, name="Other")
    _running_assignment(client, pid, other, "theirs")
    chat(client, dev["id"], "TOOL work_notes")
    wait_for(lambda: idle(client, pid))
    (result,) = _tool_results(client, dev["id"])
    assert result["is_error"] and "you have none" in result["result"]
    manager = agents_by_slug(client, pid)["manager"]
    chat(client, manager["id"], "TOOL work_notes")
    wait_for(lambda: idle(client, pid))
    assert "not available" in _tool_results(client, manager["id"])[-1]["result"]


def test_notes_survive_compression_verbatim(client, project):
    from plainboard_protocol import JobState
    from test_flow import job_tick

    pid = project["id"]
    make_dev(client, pid, context_limit_tokens=5000, keep_recent_messages=1)
    manager = agents_by_slug(client, pid)["manager"]
    steps = ["TOOL write_work_notes " + json.dumps({"text": "- bug in a.py:12\n- open: b.py"}), "TOOL start_job " + json.dumps({"command": "make a"})]
    chat(client, manager["id"], "TOOL delegate " + json.dumps({"agent": "dev", "title": "Review", "instructions": "\n".join(steps)}))
    dev = agents_by_slug(client, pid)["dev"]

    def end_job(tail: str) -> None:
        wait_for(lambda: idle(client, pid) and runner(client).jobs[runner(client).job_specs[-1].id].running)
        runner(client).jobs[runner(client).job_specs[-1].id] = JobState(running=False, exit_code=0, log_path="/jobs/x.log", tail=tail)
        job_tick(client)

    end_job("SAY grown\nCONTEXT 9000\nTOOL start_job " + json.dumps({"command": "make b"}))
    wait_for(lambda: idle(client, pid) and conversation(client, dev["id"])["compressions"] == 1)
    end_job("SAY more\nCOMPACT 190000")
    wait_for(lambda: idle(client, pid) and client.get(f"/api/projects/{pid}/assignments").json()[0]["status"] == "done")
    spec = [s for s in runner(client).specs if "Go on with assignment" in s.prompt][-1]
    assert spec.resume_session_id is None and "<work_notes>\n- bug in a.py:12\n- open: b.py\n</work_notes>" in spec.prompt
    # A compaction by the provider in the middle of a turn: the agent is told its notes are intact.
    assert any("working notes (29 chars) are intact" in text for _, text in runner(client).notices)


def test_a_restart_keeps_the_notes_and_the_agent_hears_of_them(client, project):
    pid = project["id"]
    dev = make_dev(client, pid)
    assignment_id = _running_assignment(client, pid, dev, "step 3 of 5", with_turn=True)
    client.portal.call(client.app_ref.state.runs._recover_interrupted)
    wait_for(lambda: idle(client, pid) and _notes(client, assignment_id) is None)
    resumed = next(s for s in runner(client).specs if "PlainBoard was restarted" in s.prompt)
    assert "Your working notes (11 chars) are intact" in resumed.prompt
