"""PlainBoard's own checks of the flow find what slows the team down, without a model."""

import json
from datetime import timedelta

from conftest import agents_by_slug, idle, wait_for
from plainboard_protocol import PullRequestCheck
from test_flow import chat, delegate, hand_over, land_tick_at, make_member, manager_of, repairs, team_with_branches, tool_results


def found(client, pid: int) -> dict[str, dict]:
    return {f["key"]: f for f in client.get(f"/api/projects/{pid}/flow-check?hours=24").json()}


def test_a_quiet_project_has_no_findings(client, project):
    pid = project["id"]
    assert found(client, pid) == {}
    chat(client, manager_of(client, pid)["id"], "TOOL flow_check")
    wait_for(lambda: idle(client, pid))
    assert tool_results(client, manager_of(client, pid)["id"])[-1] == "PlainBoard's checks found nothing in the last 24 h that slows the team down."


def test_a_base_branch_that_stays_red_and_the_checks_that_keep_failing_are_found(client, project):
    from plainboard.models import utcnow

    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a"], url="https://github.com/acme/api.git", check_command="", land_first=True)
    red = [PullRequestCheck(name="Tests", state="fail", link="https://ci/t"), PullRequestCheck(name="Emulator", state="fail")]
    start = utcnow() - timedelta(hours=3)
    for batch in range(1, 5):  # four landings, each red
        land_tick_at(client, start + timedelta(minutes=30 * batch))
        fake.commit_checks_of[f"train-{batch}"] = red
        hand_over(client, pid, fake, f"More{batch}", f"more-{batch}")
    land_tick_at(client, utcnow())
    wait_for(lambda: idle(client, pid))
    findings = found(client, pid)
    base = findings["red_base"]
    assert base["title"] == "main of api has failed its checks for 4 landings in a row" and "Tests (4x), Emulator (4x)" in base["detail"] and "Repairs so far:" in base["detail"]
    failing = findings["failing_checks"]
    assert failing["title"] == "4 of 4 check runs of api failed" and "Most often: Tests (4x), Emulator (4x). It has no quick check." in failing["detail"]
    assert "quick_check_command" in failing["action"]
    assert list(findings)[:2] == ["red_base", "failing_checks"]  # heaviest first

    # The manager gets the same, as text it can act on.
    chat(client, manager_of(client, pid)["id"], "TOOL flow_check")
    wait_for(lambda: idle(client, pid))
    text = tool_results(client, manager_of(client, pid)["id"])[-1]
    assert text.startswith("PlainBoard's checks of the last 24 h, heaviest first:\n1. main of api has failed its checks for 4 landings in a row\n   Red for ")
    assert "\n   → Work that starts now builds on a broken base." in text


def test_repairs_that_wait_stuck_branches_and_full_notes_are_found_and_a_branch_can_be_forgotten(client, project):
    from plainboard.db import sessionmaker
    from plainboard.models import AgentNote, Assignment, utcnow
    from sqlalchemy import update

    pid = project["id"]
    fake = team_with_branches(client, pid, ["feat-a", "feat-b"])
    dev2, manager = agents_by_slug(client, pid)["dev2"], manager_of(client, pid)
    # feat-b conflicts; its author is in the middle of long work, so the repair waits behind that.
    chat(client, manager["id"], delegate("dev2", "Long work", "TOOL start_job " + json.dumps({"command": "make all"})))
    wait_for(lambda: idle(client, pid) and fake.job_specs)
    fake.conflicting = {"feat-b"}
    client.portal.call(client.app_ref.state.landing.tick)
    wait_for(lambda: len(client.get(f"/api/projects/{pid}/assignments").json()) == 2)  # the repair is queued: it waits behind the long work

    async def age_everything() -> None:
        async with sessionmaker()() as s:
            await s.execute(update(Assignment).values(created_at=utcnow() - timedelta(hours=4)))
            s.add(AgentNote(project_id=pid, agent_id=dev2["id"], text="x" * 2800))
            await s.commit()

    client.portal.call(age_everything)
    findings = found(client, pid)
    assert findings["waiting_repairs"]["title"] == "1 repair for landing waits behind other work" and "(Dev2, 4 h)" in findings["waiting_repairs"]["detail"]
    assert findings["long_assignments"]["title"] == "1 assignment has been open for more than 3 h" and '"Long work"' in findings["long_assignments"]["detail"]
    assert findings["full_notes"]["detail"].startswith("Dev2 (2800 of 3000) characters.")

    # A branch PlainBoard stopped for: it is found, and the manager takes it off the list when its work landed another way.
    fake.branches["old-work"] = "sha-old"
    fake.conflicting.add("old-work")
    chat(client, manager["id"], "TOOL merge_when_green " + json.dumps({"repository": "api", "branch": "old-work"}))
    wait_for(lambda: "old-work" in tool_results(client, manager["id"])[-1])
    fake.jobs[fake.check_specs[-1].id] = fake.jobs[fake.check_specs[-1].id].model_copy(update={"running": False, "exit_code": 0})
    client.portal.call(client.app_ref.state.landing.tick)
    wait_for(lambda: "stuck" in found(client, pid))
    assert found(client, pid)["stuck"]["title"] == "1 branch is stuck: PlainBoard has stopped for it" and "forget_branch" in found(client, pid)["stuck"]["action"]
    chat(client, manager["id"], "TOOL forget_branch " + json.dumps({"repository": "api", "branch": "old-work"}))
    wait_for(lambda: "off the list" in tool_results(client, manager["id"])[-1])
    assert "Branch old-work is off the list (it was stuck)" in tool_results(client, manager["id"])[-1] and "stuck" not in found(client, pid)


def test_refused_calls_and_a_slow_quick_check_are_found(client, project):
    pid = project["id"]
    body = {"url": "https://gitlab.com/acme/api.git", "check_command": "./ci.sh", "quick_check_command": "cargo check"}
    assert client.post(f"/api/projects/{pid}/repositories", json=body).status_code == 201
    dev = make_member(client, pid)
    # The same call refused again and again: each one a step for nothing.
    chat(client, dev["id"], "\n".join(["TOOL get_todo " + json.dumps({"todo_id": 999})] * 6))
    wait_for(lambda: idle(client, pid))
    errors = found(client, pid)["tool_errors"]
    assert errors["title"] == "6 calls of PlainBoard's tools were refused" and errors["detail"].startswith("get_todo 6x (mostly: ")

    # The job that runs a repository's quick check says what it is, so its time can be measured.
    from plainboard.db import sessionmaker
    from plainboard.models import Job, utcnow
    from plainboard.services.flow_check import QUICK_CHECK_MARK
    from plainboard.tools.flow import quick_check_script

    script = quick_check_script("api", "feat-a", "abc1234", "cargo check")
    assert script.startswith(QUICK_CHECK_MARK + "api\n")

    async def three_slow_runs() -> None:
        async with sessionmaker()() as s:
            for n in range(3):
                s.add(Job(project_id=pid, agent_id=dev["id"], runner_id=f"q{n}", command=script, status="succeeded", created_at=utcnow() - timedelta(minutes=10), finished_at=utcnow() - timedelta(minutes=6)))
            await s.commit()

    client.portal.call(three_slow_runs)
    assert found(client, pid)["slow_quick_check"]["title"] == "The quick check of api is not quick: 4 min at the median"
