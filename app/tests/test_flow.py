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


# --- landing: finished branches go to the base branch together, with one check run -----------


def land_tick(client) -> None:
    client.portal.call(client.app_ref.state.landing.tick)


def team_with_branches(client, pid: int, branches: list[str], *, url: str = "https://gitlab.com/acme/api.git", check_command: str = "./ci.sh") -> FakeRunner:
    """One member per branch; each has pushed its branch and hands it over."""
    assert client.post(f"/api/projects/{pid}/repositories", json={"url": url, "check_command": check_command}).status_code == 201
    fake = runner(client)
    for index, branch in enumerate(branches):
        member = make_member(client, pid, f"Dev{index + 1}")
        fake.branches[branch] = f"sha-{branch}"
        chat(client, member["id"], "TOOL merge_when_green " + json.dumps({"repository": "api", "branch": branch}))
    wait_for(lambda: idle(client, pid) and len(runs_of(client, pid)) == len(branches))
    return fake


def finish_check(client, fake: FakeRunner, culprit: str | None = None, every: bool = False) -> None:
    """End the running check of the last train: it fails if the culprit is on board (or always)."""
    failed = every or culprit in fake.train_results[-1].merged
    fake.jobs[fake.check_specs[-1].id] = JobState(running=False, exit_code=1 if failed else 0, log_path="/jobs/check.log", tail="FAILED test_login" if failed else "all passed")
    land_tick(client)
    wait_for(lambda: idle(client, pid_of(client)))


def pid_of(client) -> int:
    return client.get("/api/projects").json()[0]["id"]


def last_update(fake: FakeRunner) -> str:
    return [s.prompt for s in fake.specs if s.toolset == "board"][-1]


def test_finished_branches_are_checked_together_and_one_check_run_lands_them_all(client, project):
    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a", "feat-b", "feat-c"])
    assert "is with PlainBoard now" in runs_of(client, pid)[0]["result_text"]  # the member is done without waiting for any check
    land_tick(client)
    (train,) = fake.train_specs
    assert train.members == ["feat-a", "feat-b", "feat-c"] and train.github is None and train.remote.url == "https://gitlab.com/acme/api.git"
    assert [c.command for c in fake.check_specs] == ["./ci.sh"]  # one check run for three branches, on any git host

    # What gets ready while the check runs waits for the next train: check runs follow the clock, not the changes.
    late = make_member(client, pid, "Late")
    fake.branches["feat-d"] = "sha-feat-d"
    chat(client, late["id"], "TOOL merge_when_green " + json.dumps({"repository": "api", "branch": "feat-d"}))
    wait_for(lambda: idle(client, pid))
    before = len(fake.specs)
    land_tick(client)
    assert len(fake.train_specs) == 1 and len(fake.specs) == before  # nobody gets a turn while it runs
    chat(client, manager_of(client, pid)["id"], "SAY ok")
    wait_for(lambda: idle(client, pid))
    assert "Branch feat-a, Dev1: being checked with 2 other branches" in last_update(fake) and "Branch feat-d, Late: goes on the next train" in last_update(fake)

    finish_check(client, fake)
    (landed,) = fake.land_specs
    assert landed.base == "main" and landed.head_sha == "train-1" and landed.delete_branches == {"feat-a": "sha-feat-a", "feat-b": "sha-feat-b", "feat-c": "sha-feat-c"}
    update = last_update(fake)
    assert "Landed on main in api: feat-a (Dev1); feat-b (Dev2); feat-c (Dev3)." in update and "for 3 branches" in update
    assert [t.members for t in fake.train_specs] == [["feat-a", "feat-b", "feat-c"], ["feat-d"]]  # the next train left at once
    finish_check(client, fake)
    assert len(fake.check_specs) == 2 and len(fake.land_specs) == 2  # four branches, two check runs


def test_a_train_waits_a_moment_for_further_branches(client, project):
    from plainboard.models import utcnow

    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a"])
    watcher = client.app_ref.state.landing
    watcher.linger = 90
    land_tick(client)
    assert not fake.train_specs  # a second member may be about to finish: one train for both is one check run
    late = make_member(client, pid, "Late")
    fake.branches["feat-b"] = "sha-feat-b"
    chat(client, late["id"], "TOOL merge_when_green " + json.dumps({"repository": "api", "branch": "feat-b"}))
    wait_for(lambda: idle(client, pid))
    client.portal.call(watcher.tick, utcnow() + timedelta(seconds=91))
    assert [t.members for t in fake.train_specs] == [["feat-a", "feat-b"]]


def test_a_failed_train_is_cut_in_halves_until_the_branch_that_breaks_it_is_found(client, project):
    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a", "feat-b", "feat-c", "feat-d"])
    land_tick(client)
    for _ in range(5):  # all four, then the halves, then the two of the half that failed
        finish_check(client, fake, culprit="feat-d")
    assert [t.members for t in fake.train_specs] == [["feat-a", "feat-b", "feat-c", "feat-d"], ["feat-a", "feat-b"], ["feat-c", "feat-d"], ["feat-c"], ["feat-d"]]
    assert [sorted(spec.delete_branches) for spec in fake.land_specs] == [["feat-a", "feat-b"], ["feat-c"]]  # the others landed meanwhile
    # Only the branch that breaks the checks goes back, to its author, with what failed.
    (repair,) = assignments(client, pid)
    assert repair["title"] == "Repair branch feat-d" and repair["agent_id"] == agents_by_slug(client, pid)["dev4"]["id"]
    prompt = next(s.prompt for s in fake.specs if "Repair branch feat-d" in s.prompt)
    assert "Assignment #1 from PlainBoard" in prompt and "FAILED test_login" in prompt and "failed round 1 of 3" in prompt
    assert runs_of(client, pid)[0]["trigger"] == "assignment"  # the repair's report wakes nobody

    # Repaired and pushed: it goes on the next train by itself.
    fake.branches["feat-d"] = "sha-feat-d-2"
    land_tick(client)
    assert fake.train_specs[-1].members == ["feat-d"] and fake.train_specs[-1].skip_unchanged == {"feat-d": "sha-feat-d"}
    finish_check(client, fake)
    assert sorted(fake.land_specs[-1].delete_branches) == ["feat-d"]


def test_when_both_halves_fail_landing_stops_instead_of_burning_check_runs(client, project):
    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a", "feat-b", "feat-c", "feat-d"])
    land_tick(client)
    for _ in range(3):
        finish_check(client, fake, every=True)  # e.g. the base branch is broken, or a check fails at random
    assert len(fake.check_specs) == 3 and not assignments(client, pid)  # no fourth run, nobody is blamed
    update = last_update(fake)
    assert "Landing in api has stopped" in update and "both of its halves" in update and "main itself" in update
    assert all(branch in update for branch in ("feat-a", "feat-b", "feat-c", "feat-d"))
    before = len(fake.train_specs)
    land_tick(client)
    assert len(fake.train_specs) == before


def test_a_repair_without_a_push_and_a_branch_that_keeps_failing_go_to_the_manager(client, project):
    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a"])
    land_tick(client)
    finish_check(client, fake, culprit="feat-a")
    wait_for(lambda: idle(client, pid) and assignments(client, pid) and assignments(client, pid)[0]["status"] == "done")
    land_tick(client)  # repaired, says its author, but nothing new was pushed
    wait_for(lambda: idle(client, pid) and "still fails, and nothing new was pushed" in last_update(fake))
    assert len(fake.check_specs) == 1  # no check run for the same commit again

    # The manager hands it over again; it fails with new commits each time.
    for round_no in (1, 2, 3):
        fake.branches["feat-a"] = f"try-{round_no}"
        if round_no == 1:
            chat(client, manager_of(client, pid)["id"], "TOOL merge_when_green " + json.dumps({"repository": "api", "branch": "feat-a"}))
            wait_for(lambda: idle(client, pid))
        land_tick(client)
        finish_check(client, fake, culprit="feat-a")
        wait_for(lambda: idle(client, pid))
    assert "3 times now" in last_update(fake) and "PlainBoard has stopped for this branch" in last_update(fake)
    assert len(assignments(client, pid)) == 3  # two more repairs; the third failure is not handed back


def test_a_branch_that_conflicts_stays_behind_and_the_rest_goes(client, project):
    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a", "feat-b", "feat-c"])
    fake.conflicting.add("feat-b")
    fake.on_base.add("feat-c")
    land_tick(client)
    wait_for(lambda: idle(client, pid) and assignments(client, pid))
    assert fake.train_results[-1].merged == {"feat-a": "sha-feat-a"} and len(fake.check_specs) == 1
    (repair,) = assignments(client, pid)
    assert repair["title"] == "Resolve the conflicts of branch feat-b"
    assert "feat-c (Dev3) is on main in api already" in last_update(fake)


def test_a_check_lost_with_the_runner_or_a_base_branch_that_moved_means_another_train(client, project):
    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a"])
    land_tick(client)
    fake.jobs.clear()  # the runner restarted
    land_tick(client)
    assert len(fake.train_specs) == 2 and len(fake.check_specs) == 2
    fake.land_error = "! [rejected] main -> main (fetch first)"
    finish_check(client, fake)
    assert len(fake.train_specs) == 3  # assembled and checked again on the new base
    fake.land_error = "remote: GitLab: You are not allowed to push code to protected branches"
    finish_check(client, fake)
    assert "could not be pushed to main" in last_update(fake) and len(fake.train_specs) == 3


def test_on_github_a_pull_request_for_the_whole_train_is_checked_and_merged(client, project):
    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a", "feat-b"], url="https://github.com/acme/api.git", check_command="")
    land_tick(client)
    (train,) = fake.train_specs
    assert train.github == "acme/api" and train.branch.startswith("plainboard/train-") and "feat-a" in train.body and not fake.check_specs
    number = fake.train_results[-1].number
    before = len(fake.specs)
    land_tick(client)  # its checks are still running
    assert len(fake.specs) == before and not fake.merged

    fake.pulls[("acme/api", number)] = fake.pulls[("acme/api", number)].model_copy(update={"checks": [PullRequestCheck(name="ci / test", state="pass")]})
    land_tick(client)
    wait_for(lambda: idle(client, pid) and "Landed on main in api: feat-a (Dev1); feat-b (Dev2)" in last_update(fake))
    ((repo, merged, options),) = fake.merged
    assert (repo, merged) == ("acme/api", number) and options["methods"][0] == "merge" and options["delete_branches"] == {"feat-a": "sha-feat-a", "feat-b": "sha-feat-b"}


def test_on_github_failed_checks_of_a_single_branch_go_back_with_their_names(client, project):
    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a"], url="https://github.com/acme/api.git", check_command="")
    land_tick(client)
    number = fake.train_results[-1].number
    checks = [PullRequestCheck(name="ci / lint", state="fail", link="https://ci/lint"), PullRequestCheck(name="ci / test", state="pending")]
    fake.pulls[("acme/api", number)] = fake.pulls[("acme/api", number)].model_copy(update={"checks": checks})
    land_tick(client)  # one check still runs: a failure is reported once, with all of them
    assert not assignments(client, pid)
    checks[1] = PullRequestCheck(name="ci / test", state="fail", link="https://ci/test")
    fake.pulls[("acme/api", number)] = fake.pulls[("acme/api", number)].model_copy(update={"checks": checks})
    land_tick(client)
    wait_for(lambda: idle(client, pid) and assignments(client, pid))
    prompt = next(s.prompt for s in fake.specs if "Repair branch feat-a" in s.prompt)
    assert "ci / lint: https://ci/lint" in prompt and "ci / test: https://ci/test" in prompt and fake.closed == [number]


def test_branches_can_only_be_handed_over_where_plainboard_can_check_them(client, project):
    pid = project["id"]
    dev = make_member(client, pid)
    assert client.post(f"/api/projects/{pid}/repositories", json={"url": "https://gitlab.com/acme/api.git"}).status_code == 201
    hand = "TOOL merge_when_green " + json.dumps({"repository": "api", "branch": "feat-a"})
    chat(client, dev["id"], hand)
    wait_for(lambda: idle(client, pid))
    assert "has no check command and is not on GitHub" in runs_of(client, pid)[0]["result_text"]

    # The manager gives the repository a check command: now it works on any host.
    chat(client, manager_of(client, pid)["id"], "TOOL update_repository " + json.dumps({"repository": "api", "check_command": "make check"}))
    wait_for(lambda: idle(client, pid))
    chat(client, dev["id"], hand)
    wait_for(lambda: idle(client, pid))
    assert "is not on the remote of api: push it first" in runs_of(client, pid)[0]["result_text"]
    runner(client).branches["feat-a"] = "a1"
    chat(client, dev["id"], hand)
    wait_for(lambda: idle(client, pid))
    assert "is with PlainBoard now" in runs_of(client, pid)[0]["result_text"]


def test_a_ticket_stays_in_flight_until_its_branch_has_landed(client, project):
    pid = project["id"]
    make_member(client, pid)
    assert client.post(f"/api/projects/{pid}/repositories", json={"url": "https://gitlab.com/acme/api.git", "check_command": "./ci.sh"}).status_code == 201
    ticket = client.post(f"/api/projects/{pid}/tasks", json={"title": "Feature", "status": "todo"}).json()
    fake = runner(client)
    fake.branches["feat-a"] = "a1"
    order = delegate("dev", "Add it", "TOOL merge_when_green " + json.dumps({"repository": "api", "branch": "feat-a"}), ticket_number=1)
    chat(client, manager_of(client, pid)["id"], order)
    wait_for(lambda: idle(client, pid) and assignments(client, pid) and assignments(client, pid)[0]["status"] == "done")
    wait_for(lambda: idle(client, pid))
    assert "Branch feat-a for #1, Dev: goes on the next train" in last_update(fake) and "Board check" not in last_update(fake)
    land_tick(client)
    finish_check(client, fake)
    assert "Landed on main in api: feat-a (Dev, ticket #1)" in last_update(fake)
    comments = client.get(f"/api/tasks/{ticket['id']}").json()["comments"]
    assert any("Branch feat-a is on main (one check run" in c["body"] for c in comments)


def test_checks_count_only_when_all_have_finished():
    from plainboard.orchestration.landing import NO_CHECKS_GRACE_SECONDS, base_moved, checks_result

    def pull(checks: dict[str, str]) -> PullRequestState:
        return PullRequestState(state="open", checks=[PullRequestCheck(name=name, state=result) for name, result in checks.items()])

    assert checks_result(pull({"a": "fail", "b": "pending"}), 600) == "pending"
    assert checks_result(pull({"a": "fail", "b": "pass"}), 600) == "failed"
    assert checks_result(pull({"a": "pass"}), 5) == "passed"
    assert checks_result(pull({}), NO_CHECKS_GRACE_SECONDS - 1) == "pending"  # they may not have appeared yet
    assert checks_result(pull({}), NO_CHECKS_GRACE_SECONDS) == "passed"  # a repository without checks
    assert base_moved("! [rejected] main -> main (fetch first)") and not base_moved("protected branch hook declined")


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
