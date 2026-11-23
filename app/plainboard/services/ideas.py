"""A project's ideas: a room for planning. Collecting and sorting them starts nothing; only a person turns
them into tickets (one idea at once, or a proposal of tickets planned from many, ``orchestration/ideas.py``).

The model that sorts them puts each into a topic (an island on the board of ideas), notes similar ones and
duplicates (``IdeaLink``) and writes a short hint. An idea that became a ticket is kept, marked as taken
with its ticket; only a person deletes one, on purpose.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..events import bus
from ..models import Agent, Idea, IdeaLink, IdeaStatus, IdeaTopic, InboxItem, Project, Task, TaskStatus, utcnow
from ..schemas import IdeaCreate, IdeaOut, IdeaPlanApply, IdeaTicketIn, IdeaTopicIn, IdeaTopicOut, LeftOutIdea, PlannedTicket, TaskCreate
from . import board as board_service
from .errors import NotFound, ServiceError
from .util import truncate

COLORS = 10  # the design's --chart-1..10
PENDING, SORTED, FAILED = "pending", "sorted", "failed"
SIMILAR, DUPLICATE = "similar", "duplicate"
SORT_TEXT = 600  # what the sorting model reads of an idea
PLAN_TEXT = 1500  # ... and the planning one
PLAN_TICKETS = 200  # the board's tickets the planning model is shown, newest first


# --- reading -----------------------------------------------------------------------------------


async def get(session: AsyncSession, project_id: int, idea_id: int) -> Idea:
    idea = await session.get(Idea, idea_id)
    if idea is None or idea.project_id != project_id:
        raise NotFound("Idea not found")
    return idea


async def get_topic(session: AsyncSession, project_id: int, topic_id: int) -> IdeaTopic:
    topic = await session.get(IdeaTopic, topic_id)
    if topic is None or topic.project_id != project_id:
        raise NotFound("Topic not found")
    return topic


async def topics(session: AsyncSession, project_id: int) -> list[IdeaTopic]:
    return list((await session.scalars(select(IdeaTopic).where(IdeaTopic.project_id == project_id).order_by(IdeaTopic.id))).all())


async def open_ideas(session: AsyncSession, project_id: int) -> list[Idea]:
    query = select(Idea).where(Idea.project_id == project_id, Idea.status == IdeaStatus.OPEN).order_by(Idea.id)
    return list((await session.scalars(query)).all())


async def listed(session: AsyncSession, project_id: int, *, closed: bool = False) -> list[Idea]:
    query = select(Idea).where(Idea.project_id == project_id).order_by(Idea.id)
    if not closed:
        query = query.where(Idea.status == IdeaStatus.OPEN)
    return list((await session.scalars(query)).all())


async def outs(session: AsyncSession, ideas: list[Idea]) -> list[IdeaOut]:
    """The ideas as the API shows them: with their ticket's number and the open ideas they are linked to."""
    if not ideas:
        return []
    ids = [i.id for i in ideas]
    links = (await session.scalars(select(IdeaLink).where(or_(IdeaLink.idea_id.in_(ids), IdeaLink.other_id.in_(ids))))).all()
    linked = {link.idea_id for link in links} | {link.other_id for link in links}
    still_open = set((await session.scalars(select(Idea.id).where(Idea.id.in_(linked), Idea.status == IdeaStatus.OPEN))).all()) if linked else set()
    task_ids = {i.task_id for i in ideas if i.task_id}
    numbers = dict((await session.execute(select(Task.id, Task.number).where(Task.id.in_(task_ids)))).all()) if task_ids else {}
    result = []
    for idea in ideas:
        out = IdeaOut.model_validate(idea)
        out.task_number = numbers.get(idea.task_id) if idea.task_id else None
        similar: list[int] = []
        for link in links:
            if link.idea_id == idea.id and link.kind == DUPLICATE and link.other_id in still_open:
                out.duplicate_of = link.other_id
            elif link.idea_id == idea.id and link.other_id in still_open:
                similar.append(link.other_id)
            elif link.other_id == idea.id and link.idea_id in still_open:
                similar.append(link.idea_id)
        out.similar = sorted(set(similar) - {out.duplicate_of})
        result.append(out)
    return result


async def publish(session: AsyncSession, project_id: int, ideas: list[Idea]) -> None:
    for out in await outs(session, ideas):
        bus.publish(project_id, "idea.upserted", out)


def publish_topic(topic: IdeaTopic) -> None:
    bus.publish(topic.project_id, "idea_topic.upserted", IdeaTopicOut.model_validate(topic))


# --- changing ----------------------------------------------------------------------------------


async def create(session: AsyncSession, project_id: int, data: IdeaCreate, author: str) -> Idea:
    if data.topic_id is not None:
        await get_topic(session, project_id, data.topic_id)
    idea = Idea(
        project_id=project_id,
        title=data.title.strip(),
        text=data.text.strip(),
        author=author[:100],
        topic_id=data.topic_id,
        x=data.x,
        y=data.y,
        sort_state=SORTED if data.topic_id is not None else PENDING,
    )
    session.add(idea)
    await session.commit()
    await publish(session, project_id, [idea])
    return idea


async def update(session: AsyncSession, idea: Idea, fields: dict[str, Any]) -> tuple[Idea, bool]:
    """Returns the idea and whether it waits to be sorted again (its text changed and no topic holds it)."""
    if "topic_id" in fields:
        if fields["topic_id"] is not None:
            await get_topic(session, idea.project_id, fields["topic_id"])
        idea.sort_state = SORTED if fields["topic_id"] is not None else idea.sort_state
    for key in ("text", "title"):
        if fields.get(key) is not None:
            fields[key] = fields[key].strip()
    resort = fields.get("text") not in (None, idea.text) and fields.get("topic_id", idea.topic_id) is None and idea.status == IdeaStatus.OPEN
    for key, value in fields.items():
        if value is not None or key in ("topic_id", "x", "y"):
            setattr(idea, key, value)
    if resort:
        idea.sort_state = PENDING
    await session.commit()
    await publish(session, idea.project_id, [idea])
    return idea, resort


async def remove(session: AsyncSession, idea: Idea) -> None:
    """Deleted on purpose by a person: gone for good, with its links."""
    project_id, idea_id = idea.project_id, idea.id
    others = await _linked(session, [idea_id])
    await session.delete(idea)
    await session.commit()
    bus.publish(project_id, "idea.deleted", {"id": idea_id})
    await _publish_linked(session, project_id, [idea_id], others)


async def _linked(session: AsyncSession, ids: list[int]) -> set[int]:
    query = select(IdeaLink.idea_id, IdeaLink.other_id).where(or_(IdeaLink.idea_id.in_(ids), IdeaLink.other_id.in_(ids)))
    return {a for pair in (await session.execute(query)).all() for a in pair} - set(ids)


async def _publish_linked(session: AsyncSession, project_id: int, ids: list[int], others: set[int] | None = None) -> None:
    """Ideas linked to these: what they show of their links changed."""
    others = await _linked(session, ids) if others is None else others
    if others:
        await publish(session, project_id, list((await session.scalars(select(Idea).where(Idea.id.in_(others), Idea.status == IdeaStatus.OPEN))).all()))


async def mark_all_pending(session: AsyncSession, project_id: int) -> int:
    """'Sort again': every open idea waits for the model. Returns how many."""
    ideas = await open_ideas(session, project_id)
    for idea in ideas:
        idea.sort_state = PENDING
    await session.commit()
    await publish(session, project_id, ideas)
    return len(ideas)


async def _free_color(session: AsyncSession, project_id: int) -> int:
    used = list((await session.scalars(select(IdeaTopic.color).where(IdeaTopic.project_id == project_id))).all())
    return next((c for c in range(COLORS) if c not in used), len(used) % COLORS)


async def create_topic(session: AsyncSession, project_id: int, data: IdeaTopicIn) -> IdeaTopic:
    if not data.name:
        raise ServiceError("A topic needs a name.")
    topic = IdeaTopic(project_id=project_id, name=data.name.strip(), color=data.color if data.color is not None else await _free_color(session, project_id), x=data.x, y=data.y)
    session.add(topic)
    await session.commit()
    publish_topic(topic)
    return topic


async def update_topic(session: AsyncSession, topic: IdeaTopic, data: IdeaTopicIn) -> IdeaTopic:
    fields = data.model_dump(exclude_unset=True)
    if fields.get("name"):
        fields["name"] = fields["name"].strip()
    for key, value in fields.items():
        if value is not None or key in ("x", "y"):
            setattr(topic, key, value)
    await session.commit()
    publish_topic(topic)
    return topic


async def remove_topic(session: AsyncSession, topic: IdeaTopic) -> None:
    """Its ideas stay, unsorted."""
    project_id, topic_id = topic.project_id, topic.id
    ideas = list((await session.scalars(select(Idea).where(Idea.topic_id == topic_id))).all())
    for idea in ideas:
        idea.topic_id = None
    await session.delete(topic)
    await session.commit()
    bus.publish(project_id, "idea_topic.deleted", {"id": topic_id})
    await publish(session, project_id, [i for i in ideas if i.status == IdeaStatus.OPEN])


async def _drop_empty_topics(session: AsyncSession, project_id: int) -> list[int]:
    """Topics without an open idea; not committed."""
    used = select(Idea.topic_id).where(Idea.project_id == project_id, Idea.status == IdeaStatus.OPEN, Idea.topic_id.is_not(None))
    empty = list((await session.scalars(select(IdeaTopic).where(IdeaTopic.project_id == project_id, IdeaTopic.id.not_in(used)))).all())
    for topic in empty:
        await session.delete(topic)
    return [t.id for t in empty]


# --- the model's answers -----------------------------------------------------------------------


def parse_json(text: str) -> dict[str, Any]:
    """The JSON object in a model's answer: also inside a code fence, with words around it, or with trailing
    commas. ValueError if there is none."""
    text = (text or "").strip()
    candidates = [m.group(1) for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.S)]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    candidates.append(text)
    for candidate in candidates:
        for attempt in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
            try:
                value = json.loads(attempt)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    raise ValueError("no JSON object in the answer")


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("#").isdigit():
        return int(value.strip().lstrip("#"))
    return None


def _ints(value: Any) -> list[int]:
    return [n for n in (_int(v) for v in (value if isinstance(value, list) else [value])) if n is not None]


def _str(value: Any, limit: int) -> str:
    return truncate(value.strip(), limit) if isinstance(value, str) else ""


# --- sorting -----------------------------------------------------------------------------------

SORT_SYSTEM = """\
You sort the ideas of a software project into topics, for a board of ideas. You only sort: you plan and \
start nothing.

Answer with one JSON object and nothing else:
{"topics": [{"name": "...", "hint": "..."}],
 "ideas": [{"id": 12, "topic": "...", "similar": [5], "duplicate_of": null, "hint": "..."}]}

- "ideas": one entry for every idea under "Sort these", by its id.
- "topic": the name of an existing topic if the idea fits it (exactly as written), else a new one: one to \
three words, in the language of the ideas. Prefer few, clear topics (at most about 10 in all).
- "similar": ids of other ideas about nearly the same thing. "duplicate_of": the id of an idea that says the \
same, if there is one. Only ids from this message.
- "hint": a few words for the person, only where there is something to say, e.g. "similar to #5" or \
"asks for details: which payment providers?". Else "".
- "topics": every topic you used, with a short hint about it, e.g. "3 ideas about payment".
"""


def _idea_line(idea: Idea, limit: int) -> str:
    text = " ".join(idea.text.split())
    head = f"{idea.title.strip()}: " if idea.title.strip() else ""
    return f"#{idea.id} {truncate(head + text, limit)}"


def sort_prompt(project: Project, existing: list[IdeaTopic], ideas: list[Idea], batch: list[Idea], full: bool) -> str:
    names = {t.id: t.name for t in existing}
    batch_ids = {i.id for i in batch}
    parts = [f"Project: {project.name}" + (f"\n{truncate(project.description, 1000)}" if project.description.strip() else "")]
    if existing:
        word = "Topics so far (keep, rename or merge them as fits)" if full else "Topics"
        parts.append(f"{word}:\n" + "\n".join(f"- {t.name}" + (f" ({t.hint})" if t.hint else "") for t in existing))
    others = [i for i in ideas if i.id not in batch_ids]
    if others:
        parts.append("Ideas already sorted (for topics, similar ideas and duplicates):\n" + "\n".join(f"{_idea_line(i, 200)} [{names.get(i.topic_id, 'no topic')}]" for i in others[-300:]))
    parts.append("Sort these:\n" + "\n".join(_idea_line(i, SORT_TEXT) for i in batch))
    return "\n\n".join(parts)


async def sort_failed(session: AsyncSession, project_id: int, ids: list[int]) -> None:
    ideas = list((await session.scalars(select(Idea).where(Idea.id.in_(ids), Idea.sort_state == PENDING))).all())
    for idea in ideas:
        idea.sort_state = FAILED
    await session.commit()
    await publish(session, project_id, ideas)


async def apply_sort(session: AsyncSession, project_id: int, ids: list[int], data: dict[str, Any], full: bool) -> None:
    """Store what the model answered for the ideas ``ids``. What it got wrong is left out: an idea it did not
    sort stays unsorted (failed), a link to an id it made up is not made."""
    batch = {i.id: i for i in (await session.scalars(select(Idea).where(Idea.id.in_(ids), Idea.status == IdeaStatus.OPEN))).all()}
    valid = set((await session.scalars(select(Idea.id).where(Idea.project_id == project_id, Idea.status == IdeaStatus.OPEN))).all())
    by_name = {t.name.casefold(): t for t in await topics(session, project_id)}
    touched: dict[int, IdeaTopic] = {}

    async def topic_named(name: str) -> IdeaTopic:
        topic = by_name.get(name.casefold())
        if topic is None:
            topic = IdeaTopic(project_id=project_id, name=name, color=await _free_color(session, project_id))
            session.add(topic)
            await session.flush()
            by_name[name.casefold()] = topic
        return topic

    done: set[int] = set()
    items = data.get("ideas")
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or (idea := batch.get(_int(item.get("id")) or -1)) is None or idea.id in done:
            continue
        done.add(idea.id)
        name = _str(item.get("topic"), 100)
        if name:
            topic = await topic_named(name)
            idea.topic_id = topic.id
            touched[topic.id] = topic
        idea.hint = _str(item.get("hint"), 300)
        idea.sort_state = SORTED
        await session.execute(delete(IdeaLink).where(IdeaLink.idea_id == idea.id))
        duplicate = _int(item.get("duplicate_of"))
        if duplicate in valid and duplicate != idea.id:
            session.add(IdeaLink(project_id=project_id, idea_id=idea.id, other_id=duplicate, kind=DUPLICATE))
        for other in dict.fromkeys(_ints(item.get("similar"))):
            if other in valid and other not in (idea.id, duplicate):
                session.add(IdeaLink(project_id=project_id, idea_id=idea.id, other_id=other, kind=SIMILAR))
    for idea in batch.values():
        if idea.id not in done and idea.sort_state == PENDING:
            idea.sort_state = FAILED
    named = data.get("topics")
    for item in named if isinstance(named, list) else []:
        if isinstance(item, dict) and (topic := by_name.get(_str(item.get("name"), 100).casefold())) is not None and isinstance(item.get("hint"), str):
            topic.hint = _str(item["hint"], 300)
            touched[topic.id] = topic
    gone = await _drop_empty_topics(session, project_id) if full else []
    await session.commit()
    for topic_id in gone:
        bus.publish(project_id, "idea_topic.deleted", {"id": topic_id})
    for topic in touched.values():
        if topic.id not in gone:
            publish_topic(topic)
    await publish(session, project_id, list(batch.values()))
    await _publish_linked(session, project_id, list(done))


# --- planning tickets --------------------------------------------------------------------------

PLAN_SYSTEM = """\
You plan tickets for a software project from ideas people collected. The person reviews your proposal \
and creates the tickets; you start nothing.

Do not make one ticket per idea: merge ideas that belong together, split an idea that holds several \
independent pieces of work, leave out duplicates and ideas already covered by an existing ticket. Every \
ticket is a piece of work a developer can take on: a clear title, a description of what is wanted and why \
(in the language of the ideas, Markdown), a priority (low, medium, high, urgent) and a few labels (e.g. \
frontend, backend).

Answer with one JSON object and nothing else:
{"tickets": [{"title": "...", "description": "...", "priority": "medium", "labels": ["..."], "idea_ids": [3, 7],
  "customer_title": "", "customer_description": "", "customer_texts": {}}],
 "left_out": [{"idea_id": 5, "reason": "duplicate of #3"}]}

- "idea_ids": the ideas that went into the ticket; an idea may go into several tickets.
- "left_out": every idea that went into no ticket, with a short reason (duplicate, covered by ticket #12, \
too vague, ...).
- Customer texts: only for a ticket customers would notice (a feature, a visible fix). "customer_title" \
(at most 150 characters) and "customer_description" (1-2 short sentences, at most 400) in the project's \
default customer language, simple words for end users; "customer_texts" holds the other customer languages: \
{"<code>": {"title": "...", "description": "..."}}. For other tickets leave them empty.
"""


def plan_prompt(project: Project, tickets: list[Task], ideas: list[Idea], names: dict[int, str]) -> str:
    languages = list(project.customer_languages or ["en"])
    parts = [
        f"Project: {project.name}" + (f"\n{truncate(project.description, 2000)}" if project.description.strip() else ""),
        f"Customer languages: {languages[0]} (default)" + (", " + ", ".join(languages[1:]) if languages[1:] else ""),
    ]
    if tickets:
        parts.append("Tickets on the board (do not plan these again):\n" + "\n".join(f"#{t.number} [{t.status}] {t.title}" for t in tickets))
    parts.append("Ideas to plan from:\n" + "\n".join(f"{_idea_line(i, PLAN_TEXT)}" + (f" [{names[i.topic_id]}]" if i.topic_id in names else "") for i in ideas))
    return "\n\n".join(parts)


async def plan_context(session: AsyncSession, project: Project, ideas: list[Idea]) -> str:
    query = select(Task).where(Task.project_id == project.id, Task.archived_at.is_(None)).order_by(Task.number.desc()).limit(PLAN_TICKETS)
    tickets = list(reversed((await session.scalars(query)).all()))
    names = {t.id: t.name for t in await topics(session, project.id)}
    return plan_prompt(project, tickets, ideas, names)


async def selection(session: AsyncSession, project_id: int, idea_ids: list[int] | None, topic_id: int | None) -> list[Idea]:
    """The open ideas to plan from: these, a topic's, or all."""
    query = select(Idea).where(Idea.project_id == project_id, Idea.status == IdeaStatus.OPEN).order_by(Idea.id)
    if idea_ids is not None:
        query = query.where(Idea.id.in_(idea_ids))
    elif topic_id is not None:
        await get_topic(session, project_id, topic_id)
        query = query.where(Idea.topic_id == topic_id)
    ideas = list((await session.scalars(query)).all())
    if not ideas:
        raise ServiceError("There are no open ideas to plan from.")
    return ideas


def parse_plan(data: dict[str, Any], project: Project, idea_ids: list[int]) -> tuple[list[PlannedTicket], list[LeftOutIdea]]:
    """The model's proposal, cleaned: only ideas of the selection, valid fields; an idea it placed nowhere is
    listed as left out without a reason."""
    chosen = set(idea_ids)
    languages = list(project.customer_languages or ["en"])
    tickets: list[PlannedTicket] = []
    items = data.get("tickets")
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not (title := _str(item.get("title"), 300)):
            continue
        texts: dict[str, dict[str, str]] = {}
        raw = item.get("customer_texts")
        for code, text in (raw.items() if isinstance(raw, dict) else []):
            if code in languages[1:] and isinstance(text, dict) and _str(text.get("title"), 150):
                texts[code] = {"title": _str(text.get("title"), 150), "description": _str(text.get("description"), 400)}
        labels = item.get("labels")
        try:
            tickets.append(
                PlannedTicket(
                    title=title,
                    description=item.get("description") if isinstance(item.get("description"), str) else "",
                    priority=item.get("priority") if item.get("priority") in ("low", "medium", "high", "urgent") else "medium",
                    labels=[_str(label, 50) for label in labels if isinstance(label, str) and label.strip()][:6] if isinstance(labels, list) else [],
                    customer_title=_str(item.get("customer_title"), 150),
                    customer_description=_str(item.get("customer_description"), 400) if _str(item.get("customer_title"), 150) else "",
                    customer_texts=texts,
                    idea_ids=[i for i in dict.fromkeys(_ints(item.get("idea_ids"))) if i in chosen],
                )
            )
        except ValidationError:
            continue
    used = {i for t in tickets for i in t.idea_ids}
    left_out: dict[int, LeftOutIdea] = {}
    items = data.get("left_out")
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and (idea_id := _int(item.get("idea_id"))) in chosen and idea_id not in used:
            left_out.setdefault(idea_id, LeftOutIdea(idea_id=idea_id, reason=_str(item.get("reason"), 300)))
    for idea_id in idea_ids:
        if idea_id not in used:
            left_out.setdefault(idea_id, LeftOutIdea(idea_id=idea_id))
    if not tickets:
        raise ValueError("the proposal holds no ticket")
    return tickets, list(left_out.values())


# --- into tickets ------------------------------------------------------------------------------


async def _open_for_taking(session: AsyncSession, project_id: int, ids: list[int]) -> dict[int, Idea]:
    found = {i.id: i for i in (await session.scalars(select(Idea).where(Idea.id.in_(ids), Idea.project_id == project_id))).all()} if ids else {}
    if missing := [i for i in ids if i not in found]:
        raise NotFound(f"Idea {missing[0]} not found")
    if closed := [i for i in found.values() if i.status != IdeaStatus.OPEN]:
        raise ServiceError(f"Idea {closed[0].id} is no longer open: it was {closed[0].status}.", 409)
    return found


def _tell_manager(session: AsyncSession, manager: Agent | None, tasks: list[Task], ideas: int, status: str) -> None:
    """Read with the manager's next turn; it costs no turn and starts nothing."""
    if manager is None or not tasks:
        return
    listed = ", ".join(f"#{t.number} {truncate(t.title, 80)}" for t in tasks)
    column = "To do" if status == TaskStatus.TODO else "Backlog"
    session.add(
        InboxItem(
            agent_id=manager.id,
            text=f"The user turned {ideas} idea(s) from the project's ideas into ticket(s) in {column}: {listed}. Nothing needs doing because of this note: the board's usual rules apply to them.",
        )
    )


async def _manager(session: AsyncSession, project_id: int) -> Agent | None:
    return await session.scalar(select(Agent).where(Agent.project_id == project_id, Agent.is_manager.is_(True)))


async def take_one(session: AsyncSession, idea: Idea, data: IdeaTicketIn) -> Task:
    """One idea straight into a ticket."""
    await _open_for_taking(session, idea.project_id, [idea.id])
    first_line = next((line.strip() for line in idea.text.splitlines() if line.strip()), "Idea")
    title = data.title or idea.title or truncate(first_line, 120)
    description = data.description if data.description is not None else idea.text
    (task,) = await board_service.create_tasks(session, idea.project_id, [TaskCreate(title=title, description=description, status=data.status, priority=data.priority, labels=data.labels)])
    idea.status, idea.task_id, idea.closed_at = IdeaStatus.TAKEN, task.id, utcnow()
    _tell_manager(session, await _manager(session, idea.project_id), [task], 1, data.status)
    await _drop_empty_topics_and_publish(session, idea.project_id, [idea])
    return task


async def apply_plan(session: AsyncSession, project: Project, data: IdeaPlanApply) -> tuple[list[Task], list[Idea]]:
    """The proposal as the person confirmed it: its tickets are created, their ideas marked as taken, the
    ideas in ``dismiss`` as left out with their reason. All checked before anything is written."""
    taken_ids = list(dict.fromkeys(i for t in data.tickets for i in t.idea_ids))
    dismissed = {d.idea_id: d for d in data.dismiss if d.idea_id not in taken_ids}
    ideas = await _open_for_taking(session, project.id, taken_ids + list(dismissed))
    languages = list(project.customer_languages or ["en"])
    items = []
    for ticket in data.tickets:
        texts = {code: text for code, text in ticket.customer_texts.items() if code in languages[1:]}
        items.append(TaskCreate(**ticket.model_dump(exclude={"idea_ids", "customer_texts"}), customer_texts=texts, status=data.status))
    tasks = await board_service.create_tasks(session, project.id, items)
    now = utcnow()
    for ticket, task in zip(data.tickets, tasks):
        for idea_id in ticket.idea_ids:
            idea = ideas[idea_id]
            if idea.status == IdeaStatus.OPEN:
                idea.status, idea.task_id, idea.closed_at = IdeaStatus.TAKEN, task.id, now
    for idea_id, left in dismissed.items():
        idea = ideas[idea_id]
        idea.status, idea.closed_at, idea.closed_reason = IdeaStatus.DISMISSED, now, left.reason
    _tell_manager(session, await _manager(session, project.id), tasks, len(taken_ids), data.status)
    await _drop_empty_topics_and_publish(session, project.id, list(ideas.values()))
    return tasks, list(ideas.values())


async def _drop_empty_topics_and_publish(session: AsyncSession, project_id: int, ideas: list[Idea]) -> None:
    gone = await _drop_empty_topics(session, project_id)
    await session.commit()
    for topic_id in gone:
        bus.publish(project_id, "idea_topic.deleted", {"id": topic_id})
    await publish(session, project_id, ideas)
    await _publish_linked(session, project_id, [i.id for i in ideas])

