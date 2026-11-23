"""The flow view: what works where, is checked and lands (services/flow.py, GET /api/projects/{id}/flow and friends)."""

from datetime import timedelta

from conftest import ADMIN_PASSWORD, agents_by_slug


def ago(minutes: float):
    from plainboard.models import utcnow

    return utcnow() - timedelta(minutes=minutes)


def seed(client, pid: int) -> dict:
    """A repository with one train being checked (two branches), one waiting branch, one failed and repaired train, and a sweep."""
    from plainboard.db import sessionmaker
    from plainboard.models import Assignment, FlowEvent, Job, Landing, Repository, Run, Task, Train

    agent = agents_by_slug(client, pid)["manager"]["id"]

    async def go() -> dict:
        async with sessionmaker()() as s:
            repo = Repository(project_id=pid, name="api", url="https://github.com/acme/api.git", default_branch="main", check_command="make check")
            task = Task(project_id=pid, number=1, title="Retry webhooks", status="in_progress")
            s.add_all([repo, task])
            await s.flush()
            old = [Train(project_id=pid, repository_id=repo.id, mode="command", status="landed", size=1, check_seconds=s_, finished_at=ago(100 - i * 10), created_at=ago(110 - i * 10)) for i, s_ in enumerate((300, 600, 900))]
            failed = Train(project_id=pid, repository_id=repo.id, mode="command", status="failed", size=2, check_seconds=200, failed_checks="lint\ntests", finished_at=ago(60), created_at=ago(64))
            going = Train(project_id=pid, repository_id=repo.id, mode="command", status="checking", size=2, created_at=ago(4))
            s.add_all([*old, failed, going])
            await s.flush()
            a = Landing(project_id=pid, repository_id=repo.id, branch="feat/a", title="A", agent_id=agent, task_id=task.id, status="train", train_id=going.id, created_at=ago(5), failures=1)
            b = Landing(project_id=pid, repository_id=repo.id, branch="feat/b", title="B", agent_id=agent, status="train", train_id=going.id, created_at=ago(5))
            w = Landing(project_id=pid, repository_id=repo.id, branch="feat/w", title="W", agent_id=agent, status="open", created_at=ago(1))
            done = Landing(project_id=pid, repository_id=repo.id, branch="feat/old", title="Old", agent_id=agent, status="merged", train_id=old[-1].id, merged_at=ago(80), created_at=ago(120))
            s.add_all([a, b, w, done])
            await s.flush()
            repair = Assignment(project_id=pid, agent_id=agent, task_id=task.id, title="Repair feat/a", instructions="x", status="running", landing_id=a.id)
            s.add(repair)
            await s.flush()
            s.add_all([
                Run(project_id=pid, agent_id=agent, trigger="assignment", status="running", task_id=task.id, assignment_id=repair.id, started_at=ago(3), created_at=ago(3)),
                Job(project_id=pid, agent_id=agent, runner_id="j1", command="npm  run   build", status="running", created_at=ago(1)),
                FlowEvent(project_id=pid, at=ago(64), kind="train.failed", train_id=failed.id, data={"branches": ["feat/a", "feat/b"]}),
                FlowEvent(project_id=pid, at=ago(60), kind="train.repair_ordered", train_id=failed.id, agent_id=agent, assignment_id=repair.id, data={}),
                FlowEvent(project_id=pid, at=ago(60), kind="landing.back", landing_id=a.id, task_id=task.id, data={"why": "did not pass on its own (lint)", "branch": "feat/a"}),
                FlowEvent(project_id=pid, at=ago(30), kind="branches.tidied", data={"repository": "api", "branches": {"old-1": "x", "old-2": "y"}}),
                FlowEvent(project_id=pid, at=ago(20), kind="landing.stuck", landing_id=w.id, data={"branch": "feat/w", "why": "conflicts again"}),
            ])
            await s.commit()
            return {"repo": repo.id, "task": task.id, "agent": agent, "going": going.id, "failed": failed.id}

    return client.portal.call(go)


def test_the_live_state_shows_work_branches_and_the_train_that_is_checked(client, project):
    pid = project["id"]
    ids = seed(client, pid)
    live = client.get(f"/api/projects/{pid}/flow").json()

    (repo,) = live["repositories"]
    assert repo["name"] == "api" and repo["base"] == "main" and repo["typical_check_seconds"] == 600  # the median of 300, 600 and 900

    (work,) = live["work"]
    assert work["agent_id"] == ids["agent"] and work["status"] == "running" and work["task_number"] == 1
    assert work["repair"] is True and work["branch"] == "feat/a" and work["title"] == "Repair feat/a"
    assert [j["command"] for j in work["jobs"]] == ["npm run build"]

    (train,) = live["trains"]
    assert train["id"] == ids["going"] and train["status"] == "checking" and sorted(train["branches"]) == ["feat/a", "feat/b"]
    states = {b["branch"]: b["status"] for b in live["branches"]}
    assert states == {"feat/a": "checking", "feat/b": "checking", "feat/w": "waiting", "feat/old": "merged"}
    assert next(b for b in live["branches"] if b["branch"] == "feat/a")["task_number"] == 1


def test_the_history_is_paged_newest_first_and_can_be_filtered_by_repository(client, project):
    pid = project["id"]
    ids = seed(client, pid)
    first = client.get(f"/api/projects/{pid}/flow/history", params={"limit": 3}).json()
    kinds = [(i["kind"], i["id"]) for i in first["items"]]
    assert kinds[0][0] == "stuck" and kinds[1][0] == "sweep" and kinds[2] == ("train", f"train-{ids['failed']}")
    failed = first["items"][2]
    assert failed["train"]["failed_checks"] == ["lint", "tests"] and failed["train"]["branches"] == ["feat/a", "feat/b"]
    assert [r["agent_id"] for r in failed["repairs"]] == [ids["agent"]]
    assert first["items"][1]["detail"] == "old-1, old-2" and first["items"][0]["detail"] == "feat/w: conflicts again"
    assert first["next_before"] is not None

    second = client.get(f"/api/projects/{pid}/flow/history", params={"limit": 10, "before": first["next_before"]}).json()
    assert [i["train"]["status"] for i in second["items"]] == ["landed", "landed", "landed"] and second["next_before"] is None
    assert second["items"][0]["train"]["branches"] == ["feat/old"]  # of the landings that point at it

    assert client.get(f"/api/projects/{pid}/flow/history", params={"repository_id": ids["repo"] + 1}).json()["items"] == []


def test_a_ticket_shows_the_way_of_its_branch(client, project):
    pid = project["id"]
    ids = seed(client, pid)
    (branch,) = client.get(f"/api/tasks/{ids['task']}/flow").json()
    assert branch["branch"]["branch"] == "feat/a" and branch["repository"] == "api"
    assert [(s["key"], s["state"]) for s in branch["steps"]] == [("handed", "done"), ("failed", "done"), ("repair", "done"), ("checking", "now"), ("landed", "todo")]
    assert branch["steps"][1]["detail"] == "did not pass on its own (lint)"
    assert client.get("/api/tasks/9999/flow").status_code == 404


def test_a_project_without_repositories_has_work_and_no_branches(client, project):
    pid = project["id"]
    live = client.get(f"/api/projects/{pid}/flow").json()
    assert live["repositories"] == [] and live["branches"] == [] and live["trains"] == []
    assert client.get(f"/api/projects/{pid}/flow/history").json() == {"items": [], "next_before": None}


def test_a_change_of_a_branch_tells_the_ui(client, project):
    from plainboard.db import sessionmaker
    from plainboard.events import bus
    from plainboard.models import Repository, Train

    pid = project["id"]
    queue = bus.subscribe(pid)

    async def add() -> None:
        async with sessionmaker()() as s:
            repo = Repository(project_id=pid, name="api", url="https://github.com/acme/api.git")
            s.add(repo)
            await s.flush()
            s.add(Train(project_id=pid, repository_id=repo.id, mode="command"))
            await s.commit()

    client.portal.call(add)
    events = []
    while not queue.empty():
        events.append(queue.get_nowait()["type"])
    assert "flow.changed" in events
    bus.unsubscribe(pid, queue)


def test_a_viewer_may_read_the_flow(client, project):
    pid = project["id"]
    ids = seed(client, pid)
    user = client.post("/api/users", json={"username": "vera", "password": "vera-password"}).json()
    assert client.put(f"/api/projects/{pid}/members/{user['id']}", json={"role": "viewer"}).status_code == 200
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"username": "vera", "password": "vera-password"}).status_code == 200
    assert client.get(f"/api/projects/{pid}/flow").status_code == 200
    assert client.get(f"/api/projects/{pid}/flow/history").status_code == 200
    assert client.get(f"/api/tasks/{ids['task']}/flow").status_code == 200
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD}).status_code == 200
