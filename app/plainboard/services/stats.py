"""Project statistics per day (UTC) and per agent, from what the board and the runs record anyway."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Agent, Assignment, AssignmentStatus, Conversation, Message, Run, RunStatus, Task, TaskStatus, TaskTransition, utcnow
from ..schemas import StatsAgent, StatsDay, StatsOut


async def overview(session: AsyncSession, project_id: int, days: int = 30, now: datetime | None = None) -> StatsOut:
    today = (now or utcnow()).date()
    since = datetime.combine(today - timedelta(days=days - 1), datetime.min.time())
    by_day = {d.date: d for d in (StatsDay(date=(today - timedelta(days=n)).isoformat()) for n in range(days - 1, -1, -1))}

    transitions = await session.scalars(select(TaskTransition).where(TaskTransition.project_id == project_id).order_by(TaskTransition.at, TaskTransition.id))
    started: dict[int, datetime] = {}
    created: dict[int, datetime] = {}
    done_on: set[tuple[str, int]] = set()  # moved back and forth on one day: still one ticket
    cycles: list[float] = []
    for t in transitions:
        if t.from_status is None:
            created.setdefault(t.task_number, t.at)
        if t.status == TaskStatus.IN_PROGRESS:
            started.setdefault(t.task_number, t.at)
        day = by_day.get(t.at.date().isoformat())
        if day is None:
            continue
        if t.from_status is None:
            day.tickets_created += 1
        if t.status == TaskStatus.DONE and (day.date, t.task_number) not in done_on:
            done_on.add((day.date, t.task_number))
            day.tickets_done += 1
            cycles.append((t.at - started.get(t.task_number, created.get(t.task_number, t.at))).total_seconds())

    agents: dict[int, StatsAgent] = {}
    runs = await session.execute(
        select(Run.agent_id, Run.status, Run.created_at, Run.started_at, Run.finished_at, Run.input_tokens, Run.output_tokens, Run.cost_usd).where(
            Run.project_id == project_id, Run.created_at >= since
        )
    )
    for agent_id, status, created_at, started_at, finished_at, input_tokens, output_tokens, cost in runs:
        day = by_day.get(created_at.date().isoformat())
        agent = agents.setdefault(agent_id, StatsAgent(agent_id=agent_id))
        for item in (day, agent):
            if item is not None:
                item.runs += 1
                item.failed_runs += status == RunStatus.FAILED
                item.tokens += input_tokens + output_tokens
                item.cost_usd += cost
        if started_at and finished_at:
            agent.busy_seconds += int((finished_at - started_at).total_seconds())

    finished = await session.execute(
        select(Assignment.agent_id, func.count())
        .where(Assignment.project_id == project_id, Assignment.status == AssignmentStatus.DONE, Assignment.finished_at >= since)
        .group_by(Assignment.agent_id)
    )
    for agent_id, count in finished:
        agents.setdefault(agent_id, StatsAgent(agent_id=agent_id)).assignments_done = count

    open_tickets = await session.scalar(select(func.count()).select_from(Task).where(Task.project_id == project_id, Task.status != TaskStatus.DONE, Task.archived_at.is_(None)))
    return StatsOut(
        days=list(by_day.values()),
        agents=sorted(agents.values(), key=lambda a: a.runs, reverse=True),
        open_tickets=open_tickets or 0,
        median_cycle_seconds=int(median(cycles)) if cycles else None,
    )


SHELL_COMMAND = re.compile(r'"command": "((?:cd \S+ && )?)([\w./-]+)')
REPORT_TICKETS = 300


def _seconds(start: datetime | None, end: datetime | None) -> int:
    return int((end - start).total_seconds()) if start and end else 0


async def work_report(session: AsyncSession, project_id: int, days: int = 14) -> dict:
    """Where time, turns and money went, to find overhead. Numbers and tool names only: no prompts,
    messages, ticket texts or code."""
    since = utcnow() - timedelta(days=days)
    agents = list(await session.scalars(select(Agent).where(Agent.project_id == project_id).order_by(Agent.id)))
    is_manager = {a.id: a.is_manager for a in agents}

    def totals() -> dict:
        return {"runs": 0, "failed": 0, "turns": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0, "cost_usd": 0.0, "busy_seconds": 0, "wait_seconds": 0}

    def add(item: dict, run: Run) -> None:
        item["runs"] += 1
        item["failed"] += run.status == RunStatus.FAILED
        item["turns"] += run.num_turns
        for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"):
            item[key] += getattr(run, key)
        item["cost_usd"] = round(item["cost_usd"] + run.cost_usd, 4)
        item["busy_seconds"] += _seconds(run.started_at, run.finished_at)
        item["wait_seconds"] += _seconds(run.created_at, run.started_at)

    by_trigger: dict[tuple[int, str], dict] = defaultdict(totals)
    by_ticket: dict[int, dict] = defaultdict(lambda: {"manager": totals(), "members": totals()})
    no_ticket = {"manager": totals(), "members": totals()}
    for run in await session.scalars(select(Run).where(Run.project_id == project_id, Run.created_at >= since)):
        add(by_trigger[(run.agent_id, run.trigger)], run)
        side = "manager" if is_manager.get(run.agent_id) else "members"
        add((by_ticket[run.task_id] if run.task_id else no_ticket)[side], run)

    # Tool calls per agent; for shell calls also the program, to tell building from git and testing.
    tools: dict[int, Counter] = defaultdict(Counter)
    shell: dict[int, Counter] = defaultdict(Counter)
    calls = await session.execute(
        select(Conversation.agent_id, Message.content, Message.meta)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.agent_id.in_(list(is_manager)), Message.kind == "tool", Message.created_at >= since)
    )
    for agent_id, name, meta in calls:
        tools[agent_id][name] += 1
        if name == "Bash" and (found := SHELL_COMMAND.search(str((meta or {}).get("input", "")))):
            shell[agent_id][found.group(2).rsplit("/", 1)[-1]] += 1

    numbers = {t.id: t for t in await session.scalars(select(Task).where(Task.id.in_(list(by_ticket))))} if by_ticket else {}
    assignments: dict[int, list[Assignment]] = defaultdict(list)
    lengths = {"count": 0, "instruction_chars": 0, "result_chars": 0}
    statuses: Counter = Counter()
    for a in await session.scalars(select(Assignment).where(Assignment.project_id == project_id, Assignment.created_at >= since)):
        statuses[a.status] += 1
        lengths["count"] += 1
        lengths["instruction_chars"] += len(a.instructions)
        lengths["result_chars"] += len(a.result or "")
        if a.task_id:
            assignments[a.task_id].append(a)
    moves: dict[int, list[str]] = defaultdict(list)
    for t in await session.scalars(select(TaskTransition).where(TaskTransition.project_id == project_id, TaskTransition.at >= since).order_by(TaskTransition.at, TaskTransition.id)):
        moves[t.task_number].append(t.status)

    tickets = []
    for task_id, sides in by_ticket.items():
        task = numbers.get(task_id)
        work = assignments.get(task_id, [])
        tickets.append(
            {
                "ticket": task.number if task else None,
                "status": task.status if task else "deleted",
                "moves": moves.get(task.number, []) if task else [],
                "assignments": len(work),
                "assigned_agents": len({a.agent_id for a in work}),
                "manager": sides["manager"],
                "members": sides["members"],
            }
        )
    tickets.sort(key=lambda t: t["manager"]["cost_usd"] + t["members"]["cost_usd"], reverse=True)

    overview_days = (await overview(session, project_id, days)).days
    return {
        "generated_at": utcnow().isoformat() + "Z",
        "days": days,
        "agents": [
            {
                "id": a.id,
                "name": a.name,
                "role": a.role,
                "manager": a.is_manager,
                "enabled": a.enabled,
                "provider": a.provider,
                "model": a.model,
                "effort": a.effort,
                "toolset": a.toolset,
                "instruction_chars": len(a.instructions),
                "by_trigger": {trigger: item for (agent_id, trigger), item in by_trigger.items() if agent_id == a.id},
                "tool_calls": dict(tools[a.id].most_common()),
                "shell_programs": dict(shell[a.id].most_common(40)),
            }
            for a in agents
        ],
        "tickets": tickets[:REPORT_TICKETS],
        "tickets_left_out": max(len(tickets) - REPORT_TICKETS, 0),
        "without_ticket": no_ticket,
        "assignments": {"by_status": dict(statuses), **lengths},
        "per_day": [{"date": d.date, "tickets_created": d.tickets_created, "tickets_done": d.tickets_done, "runs": d.runs, "cost_usd": round(d.cost_usd, 2)} for d in overview_days],
    }
