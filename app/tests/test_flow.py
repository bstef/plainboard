"""How work flows: one piece of work per assignment, with its own context, one report and its time
measured; pull requests watched and merged without any agent's turn."""

import json
from datetime import timedelta

from conftest import FakeRunner, agents_by_slug, idle, runs_of, wait_for
from plainboard_protocol import JobState, PullRequestCheck, PullRequestState


def chat(client, agent_id: int, text: str) -> None:
    assert client.post(f"/api/agents/{agent_id}/messages", json={"text": text}).status_code == 202


def make_member(client, pid: int, name: str = "Dev") -> dict:
    resp = client.post(f"/api/projects/{pid}/agents", json={"name": name, "role": "Developer", "description": "Backend API"})
    assert resp.status_code == 201, resp.text
    return resp.json()


def runner(client) -> FakeRunner:
    return client.app_ref.state.runner


def manager_of(client, pid: int) -> dict:
    return agents_by_slug(client, pid)["manager"]


def delegate(agent: str, title: str, instructions: str, **extra) -> str:
    return "TOOL delegate " + json.dumps({"agent": agent, "title": title, "instructions": instructions, **extra})


def assignments(client, pid: int) -> list[dict]:
    return sorted(client.get(f"/api/projects/{pid}/assignments").json(), key=lambda a: a["id"])


def tool_results(client, agent_id: int) -> list[str]:
    messages = client.get(f"/api/agents/{agent_id}/conversation").json()["messages"]
    return [m["meta"]["result"] for m in messages if m["kind"] == "tool" and "result" in m["meta"]]


def job_tick(client) -> None:
    client.portal.call(client.app_ref.state.job_watcher.tick)


def pr_tick(client) -> None:
    client.portal.call(client.app_ref.state.pr_watcher.tick)


# --- one assignment, one report -------------------------------------------------------------


def test_an_assignment_stays_open_while_its_job_runs_and_reports_once(client, project):
    pid = project["id"]
    make_member(client, pid)
    manager = manager_of(client, pid)
    chat(client, manager["id"], delegate("dev", "Fix it", "TOOL start_job " + json.dumps({"command": "make test"})))
    wait_for(lambda: idle(client, pid) and runner(client).job_specs)
    (assignment,) = assignments(client, pid)
    assert assignment["status"] == "running" and assignment["result"] is None
    assert [r["trigger"] for r in runs_of(client, pid)] == ["assignment", "chat"]  # no report yet: the manager is not woken
    assert "Your assignment stays open until it ends" in runs_of(client, pid)[0]["result_text"]

    spec = runner(client).job_specs[-1]
    runner(client).jobs[spec.id] = JobState(running=False, exit_code=0, log_path="/jobs/x.log", tail="SAY All green")
    job_tick(client)
    wait_for(lambda: idle(client, pid) and assignments(client, pid)[0]["status"] == "done")
    assert assignments(client, pid)[0]["result"] == "All green"
    wait_for(lambda: any('Assignment #1 "Fix it"' in s.prompt and s.toolset == "board" for s in runner(client).specs))
    wait_for(lambda: idle(client, pid))
    report, turn, _first, _chat = runs_of(client, pid)
    assert (turn["trigger"], turn["assignment_id"]) == ("wake", assignment["id"])  # the job's result continues the assignment
    going_on = next(s.prompt for s in runner(client).specs if "Go on with assignment #1" in s.prompt)
    assert "exit code 0 after" in going_on and "So far it took" in going_on and "waiting for 1 job" in going_on
    assert report["agent_id"] == manager["id"] and report["trigger"] == "wake"
    update = runner(client).specs[-1].prompt
    assert 'Assignment #1 "Fix it" (Dev) done after' in update and "of work in 2 turns" in update and "All green" in update


def test_a_server_does_not_keep_an_assignment_open(client, project):
    pid = project["id"]
    make_member(client, pid)
    chat(client, manager_of(client, pid)["id"], delegate("dev", "Preview", "TOOL start_job " + json.dumps({"command": "npm run dev", "server": True})))
    wait_for(lambda: idle(client, pid) and assignments(client, pid) and assignments(client, pid)[0]["status"] == "done")
    assert "keeps running until someone stops it" in assignments(client, pid)[0]["result"]


def test_an_assignment_waiting_for_its_job_can_be_cancelled(client, project):
    pid = project["id"]
    make_member(client, pid)
    manager = manager_of(client, pid)
    chat(client, manager["id"], delegate("dev", "Fix it", "TOOL start_job " + json.dumps({"command": "make test"})))
    wait_for(lambda: idle(client, pid) and runner(client).job_specs)
    chat(client, manager["id"], 'TOOL cancel_assignment {"assignment_id": 1}')
    wait_for(lambda: idle(client, pid) and assignments(client, pid)[0]["status"] == "cancelled")
    assert agents_by_slug(client, pid)["dev"]["running_jobs"] == 0  # its job is stopped with it


# --- context per piece of work --------------------------------------------------------------


def test_a_member_starts_another_ticket_with_a_fresh_context(client, project):
    pid = project["id"]
    dev = make_member(client, pid)
    manager = manager_of(client, pid)
    for title in ("One", "Two"):
        client.post(f"/api/projects/{pid}/tasks", json={"title": title, "status": "todo"})
    for number, text in ((1, "first"), (1, "again"), (2, "other")):
        chat(client, manager["id"], delegate("dev", f"Work {text}", f"SAY {text}", ticket_number=number))
        wait_for(lambda: idle(client, pid) and len([a for a in assignments(client, pid) if a["status"] == "done"]) == len(assignments(client, pid)))
        wait_for(lambda: idle(client, pid))
    first, again, other = [s for s in runner(client).specs if s.toolset == "full"]
    assert first.resume_session_id is None and "This is a new context" in first.prompt
    assert again.resume_session_id == f"session-{first.run_id}" and "new context" not in again.prompt  # same ticket: same context
    assert other.resume_session_id is None and "This is a new context" in other.prompt
    assert "#2 Work again; #1 Work first" in other.prompt  # where it left off, without the conversation
    assert "Work first" not in other.prompt.split("#2 Work again; #1 Work first")[1]
    marks = [m for m in client.get(f"/api/agents/{dev['id']}/conversation").json()["messages"] if m["kind"] == "summary"]
    assert len(marks) == 1 and marks[0]["meta"]["fresh"] is True and not runner(client).completions  # nothing is summarized for it


# --- the database remembers what is ordered, not the manager ---------------------------------


def test_delegate_refuses_a_second_assignment_for_a_ticket_with_open_work(client, project):
    pid = project["id"]
    make_member(client, pid)
    make_member(client, pid, "QA")
    manager = manager_of(client, pid)
    client.post(f"/api/projects/{pid}/tasks", json={"title": "Build it", "status": "todo"})
    chat(client, manager["id"], delegate("dev", "Work", "HOLD", ticket_number=1))
    wait_for(lambda: any(r["trigger"] == "assignment" and r["status"] == "running" for r in runs_of(client, pid)))
    chat(client, manager["id"], delegate("qa", "Work", "HOLD", ticket_number=1) + chr(10) + delegate("qa", "Work", "HOLD", ticket_number=1, parallel=True))
    wait_for(lambda: len(tool_results(client, manager["id"])) == 3)
    results = tool_results(client, manager["id"])
    assert "already has open work" in results[1] and '#1 "Work" with Dev (running)' in results[1]
    assert "Assignment #2 queued for qa" in results[2]
    # Every manager turn lists what is in flight.
    prompt = [s.prompt for s in runner(client).specs if s.toolset == "board"][-1]
    assert "In flight (already ordered" in prompt and 'Assignment #1 for #1, Dev: "Work" · working' in prompt
    runner(client).released = True
    wait_for(lambda: idle(client, pid))


def test_started_work_goes_before_new_work_and_the_manager_needs_no_slot(client, project):
    pid = project["id"]
    client.put("/api/settings", json={"values": {"max_parallel_runs": 1}})
    dev, qa, ops = make_member(client, pid), make_member(client, pid, "QA"), make_member(client, pid, "Ops")
    manager = manager_of(client, pid)
    chat(client, ops["id"], "HOLD")
    wait_for(lambda: any(r["status"] == "running" for r in runs_of(client, pid)))
    # The only slot is taken, and the manager still gets its turn.
    chat(client, manager["id"], delegate("dev", "New work", "SAY new") + chr(10) + 'TOOL message_agent {"agent": "qa", "message": "SAY note"}')
    wait_for(lambda: any(r["agent_id"] == manager["id"] and r["status"] == "succeeded" for r in runs_of(client, pid)))
    assert {r["agent_id"] for r in runs_of(client, pid) if r["status"] == "queued"} == {dev["id"], qa["id"]}
    runner(client).released = True
    wait_for(lambda: idle(client, pid))
    run_of = {r["agent_id"]: str(r["id"]) for r in runs_of(client, pid) if r["agent_id"] in (dev["id"], qa["id"])}
    order = [s.run_id for s in runner(client).specs]
    assert order.index(run_of[qa["id"]]) < order.index(run_of[dev["id"]])  # queued later, but it belongs to work that has begun


# --- pull requests ---------------------------------------------------------------------------


def pull(checks: dict[str, str] | None = None, *, sha: str = "aaa", mergeable: str = "yes", state: str = "open") -> PullRequestState:
    return PullRequestState(
        state=state,
        mergeable=mergeable,
        head_sha=sha,
        url="https://github.com/acme/api/pull/7",
        title="Add feature",
        checks=[PullRequestCheck(name=name, state=result, link=f"https://ci/{name}") for name, result in (checks or {}).items()],
    )


def hand_over(client, pid: int, first: PullRequestState) -> FakeRunner:
    """A member opens PR #7 for ticket #1 and hands it to PlainBoard."""
    make_member(client, pid)
    assert client.post(f"/api/projects/{pid}/repositories", json={"url": "https://github.com/acme/api.git"}).status_code == 201
    client.post(f"/api/projects/{pid}/tasks", json={"title": "Feature", "status": "todo"})
    fake = runner(client)
    fake.pulls[("acme/api", 7)] = first
    order = delegate("dev", "Add it", "TOOL merge_when_green " + json.dumps({"repository": "api", "number": 7}), ticket_number=1)
    chat(client, manager_of(client, pid)["id"], order)
    wait_for(lambda: idle(client, pid) and assignments(client, pid) and assignments(client, pid)[0]["status"] == "done")
    wait_for(lambda: idle(client, pid))
    return fake


def test_a_pull_request_is_merged_by_plainboard_when_its_checks_pass(client, project):
    pid = project["id"]
    fake = hand_over(client, pid, pull({"ci / test": "pending"}))
    assert "is with PlainBoard now" in assignments(client, pid)[0]["result"]  # the member is done without waiting for the checks

    before = len(fake.specs)
    pr_tick(client)  # checks still running: nobody gets a turn for that
    assert len(fake.specs) == before and not fake.merged
    chat(client, manager_of(client, pid)["id"], "SAY ok")
    wait_for(lambda: idle(client, pid))
    assert "PR #7 for #1, Dev: checks running" in fake.specs[-1].prompt and "Board check" not in fake.specs[-1].prompt

    fake.pulls[("acme/api", 7)] = pull({"ci / test": "pass"})
    before = len(fake.specs)
    pr_tick(client)
    wait_for(lambda: idle(client, pid) and len(fake.specs) == before + 1)
    assert fake.merged == [("acme/api", 7)]
    assert 'PR #7 "Add feature" (Dev, ticket #1) is merged.' in fake.specs[-1].prompt
    ticket = client.get(f"/api/projects/{pid}/tasks").json()[0]
    assert any("PR #7 is merged" in c["body"] for c in client.get(f"/api/tasks/{ticket['id']}").json()["comments"])
    pr_tick(client)  # nothing left to watch
    wait_for(lambda: idle(client, pid))
    assert len(fake.specs) == before + 1


def test_failed_checks_go_back_to_the_author_all_at_once(client, project):
    pid = project["id"]
    fake = hand_over(client, pid, pull({"ci / lint": "pending", "ci / test": "pending"}))
    fake.pulls[("acme/api", 7)] = pull({"ci / lint": "fail", "ci / test": "pending"})
    before = len(fake.specs)
    pr_tick(client)  # one check still runs: the author gets every failure in one go, not one per round
    assert len(fake.specs) == before and len(assignments(client, pid)) == 1

    fake.pulls[("acme/api", 7)] = pull({"ci / lint": "fail", "ci / test": "fail"})
    pr_tick(client)
    wait_for(lambda: idle(client, pid) and len(assignments(client, pid)) == 2 and assignments(client, pid)[1]["status"] == "done")
    repair = next(s.prompt for s in fake.specs if "Assignment #2 from PlainBoard" in s.prompt)
    assert "Fix the failed checks of PR #7" in repair and "ci / lint: https://ci/ci / lint" in repair and "ci / test" in repair
    assert "failed round 1 of 3" in repair
    assert runs_of(client, pid)[0]["trigger"] == "assignment"  # the repair's report wakes nobody: PlainBoard keeps watching

    # The author pushed: a new round. This time everything passes and it is merged.
    fake.pulls[("acme/api", 7)] = pull({"ci / lint": "pass", "ci / test": "pass"}, sha="bbb")
    pr_tick(client)
    wait_for(lambda: idle(client, pid) and fake.merged == [("acme/api", 7)])


def test_a_pull_request_that_keeps_failing_goes_to_the_manager(client, project):
    pid = project["id"]
    fake = hand_over(client, pid, pull({"ci": "pending"}))
    for round_no in (1, 2, 3):
        fake.pulls[("acme/api", 7)] = pull({"ci": "fail"}, sha=f"sha{round_no}")
        pr_tick(client)
        wait_for(lambda: idle(client, pid))
    assert len(assignments(client, pid)) == 3  # the first work and two repairs: the third failure is not handed back
    update = fake.specs[-1].prompt
    assert "3 rounds in a row now" in update and "PlainBoard stopped watching it" in update and fake.specs[-1].toolset == "board"
    before = len(fake.specs)
    pr_tick(client)
    assert len(fake.specs) == before


def test_a_repair_without_a_push_does_not_leave_the_pull_request_forgotten(client, project):
    pid = project["id"]
    fake = hand_over(client, pid, pull({"ci": "pending"}))
    fake.pulls[("acme/api", 7)] = pull({"ci": "fail"})
    pr_tick(client)
    wait_for(lambda: idle(client, pid) and len(assignments(client, pid)) == 2 and assignments(client, pid)[1]["status"] == "done")
    before = len(fake.specs)
    pr_tick(client)  # same head, still red, and its author is done with it
    wait_for(lambda: idle(client, pid) and len(fake.specs) == before + 1)
    assert "finished the repair without a new push" in fake.specs[-1].prompt


def test_conflicts_and_pull_requests_merged_by_hand(client, project):
    pid = project["id"]
    fake = hand_over(client, pid, pull({"ci": "pending"}))
    fake.pulls[("acme/api", 7)] = pull({}, mergeable="conflict")
    pr_tick(client)
    wait_for(lambda: idle(client, pid) and len(assignments(client, pid)) == 2)
    assert assignments(client, pid)[1]["title"] == "Resolve the conflicts of PR #7"
    fake.pulls[("acme/api", 7)] = pull({"ci": "pass"}, sha="ccc", state="merged")  # someone merged it meanwhile
    pr_tick(client)
    wait_for(lambda: idle(client, pid) and "is merged" in fake.specs[-1].prompt)
    assert not fake.merged


def test_only_github_repositories_with_the_general_access_can_be_watched(client, project):
    pid = project["id"]
    dev = make_member(client, pid)
    assert client.post(f"/api/projects/{pid}/repositories", json={"url": "https://gitlab.com/acme/api.git"}).status_code == 201
    chat(client, dev["id"], "TOOL merge_when_green " + json.dumps({"repository": "api", "number": 3}))
    wait_for(lambda: idle(client, pid))
    assert "is not on GitHub" in runs_of(client, pid)[0]["result_text"]


def test_checks_count_only_when_all_have_finished():
    from plainboard.orchestration.pr_watcher import NO_CHECKS_GRACE_SECONDS, checks_result

    assert checks_result(pull({"a": "fail", "b": "pending"}), 600) == "pending"
    assert checks_result(pull({"a": "fail", "b": "pass"}), 600) == "failed"
    assert checks_result(pull({"a": "pass"}), 5) == "passed"
    assert checks_result(pull({}), NO_CHECKS_GRACE_SECONDS - 1) == "pending"  # they may not have appeared yet
    assert checks_result(pull({}), NO_CHECKS_GRACE_SECONDS) == "passed"  # a repository without checks


# --- time ------------------------------------------------------------------------------------


def test_durations_are_said_in_plain_words():
    from plainboard.services.timing import Ledger, command_key, duration

    assert [duration(s) for s in (45, 600, 5400, 6720, 200000)] == ["45 s", "10 min", "1 h 30 min", "1 h 52 min", "2 d 7 h"]
    ledger = Ledger(total=6720, work=540, waiting=5760, queued=420, turns=4, jobs=3)
    assert ledger.text() == "1 h 52 min: 9 min of work in 4 turns, 1 h 36 min waiting for 3 jobs, 7 min queued"
    assert Ledger(total=300, work=300, turns=1).text() == "5 min: 5 min of work in 1 turn"
    assert command_key("cd /workspaces/p/dev/api  &&  make   test") == "make test"


def test_the_same_slow_command_is_not_run_in_circles(client, project):
    from plainboard.models import Assignment, Job, JobStatus, utcnow
    from plainboard.orchestration.run_manager import RunContext

    pid = project["id"]
    dev = make_member(client, pid)
    runs = client.app_ref.state.runs

    async def scenario():
        async with runs._sm() as s:
            assignment = Assignment(project_id=pid, agent_id=dev["id"], title="Fix", instructions="x")
            s.add(assignment)
            await s.flush()
            for n in range(3):
                start = utcnow() - timedelta(minutes=90 - 25 * n)
                s.add(Job(project_id=pid, agent_id=dev["id"], assignment_id=assignment.id, runner_id=f"cd{n}", command="make test", status=JobStatus.FAILED, created_at=start, finished_at=start + timedelta(minutes=20)))
            await s.commit()
            ctx = RunContext(run_id=0, project_id=pid, agent_id=dev["id"], token="", assignment_id=assignment.id)
        refused = await runs.execute_tool(ctx, "start_job", {"command": "make test"})
        other = await runs.execute_tool(ctx, "start_job", {"command": "make lint"})
        insisted = await runs.execute_tool(ctx, "start_job", {"command": "make test", "repeat": True})
        return refused, other, insisted

    refused, other, insisted = client.portal.call(scenario)
    assert refused.is_error and "3 times already (60 min together)" in refused.text and "is a loop" in refused.text
    assert not other.is_error  # another command is another matter
    assert not insisted.is_error and "This is run 4 of it in this assignment" in insisted.text


def test_a_slow_command_says_what_it_cost_before_it_is_started_again(client, project):
    from plainboard.models import Assignment, Job, JobStatus, utcnow
    from plainboard.services import timing

    pid = project["id"]
    dev = make_member(client, pid)

    async def scenario():
        async with client.app_ref.state.runs._sm() as s:
            assignment = Assignment(project_id=pid, agent_id=dev["id"], title="Fix", instructions="x")
            s.add(assignment)
            await s.flush()
            now = utcnow()
            for n in range(2):
                start = now - timedelta(minutes=60 - 25 * n)
                s.add(Job(project_id=pid, agent_id=dev["id"], assignment_id=assignment.id, runner_id=f"ab{n}", command="make test", status=JobStatus.FAILED, created_at=start, finished_at=start + timedelta(minutes=20)))
            await s.commit()
            in_assignment = await timing.job_note(s, dev["id"], assignment.id, "make test")
            outside = await timing.job_note(s, dev["id"], None, "make test")
            unknown = await timing.job_note(s, dev["id"], assignment.id, "make lint")
            overview = await timing.overview(s, pid, 24)
            return in_assignment, outside, unknown, overview

    in_assignment, outside, unknown, overview = client.portal.call(scenario)
    assert outside == "It took about 20 min the last 2 times."
    assert in_assignment.startswith("It took about 20 min the last 2 times. This is run 3 of it in this assignment; the earlier ones took 40 min together.")
    assert "fix everything the last one showed" in in_assignment
    assert unknown == ""
    (slow,) = overview.commands
    assert (slow.command, slow.runs, slow.median, slow.total) == ("make test", 2, 1200, 2400)
    report = timing.report_text(overview, [])
    assert "40 min = 2 x 20 min: make test" in report and "Most of the time is waiting" in report
    assert "40 min waiting for jobs and checks" in timing.waiting_line(overview) or timing.waiting_line(overview) == ""
    assert "`make test` 20 min per run" in timing.facts_text(overview)
