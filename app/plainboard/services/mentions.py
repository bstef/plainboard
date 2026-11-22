"""@mentions: an agent or a person names someone with @username in a chat message, a comment or a
decision, and that person finds it in their list of mentions (and gets a browser notification while
PlainBoard is open). Only people with access to the project count (its members of any role and the
admins); any other @word, and anything in code, stays plain text.
"""

from __future__ import annotations

import re

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..events import bus
from ..models import Agent, Mention, Project, ProjectMember, Task, User, utcnow
from ..schemas import MentionOut, MentionsOut
from .util import truncate

# Not inside words, e-mail addresses or paths ("a@b", "x/@y"); a name as users.USERNAME allows.
MENTION = re.compile(r"(?<![\w@./-])@([a-z0-9][a-z0-9._-]{1,58})", re.IGNORECASE)
CODE = re.compile(r"(```|~~~).*?(?:\1|\Z)|`[^`\n]*`", re.DOTALL)
EXCERPT = 240
PAGE = 30


def usernames(text: str) -> set[str]:
    """The @names in a text, lower case, outside code (fenced blocks and `inline code`)."""
    names: set[str] = set()
    for match in MENTION.finditer(CODE.sub(" ", text or "")):
        name = match.group(1).lower()
        names |= {name, name.rstrip("._-")}  # "@anna." ends a sentence
    return names


async def people(session: AsyncSession, project_id: int) -> list[User]:
    """Who can be mentioned in a project: everyone with access to it, viewers included."""
    members = select(ProjectMember.user_id).where(ProjectMember.project_id == project_id)
    q = select(User).where(User.enabled, User.is_admin | User.id.in_(members)).order_by(func.lower(User.name), User.id)
    return list((await session.scalars(q)).all())


async def record(
    session: AsyncSession,
    project_id: int,
    text: str,
    *,
    kind: str,
    author: str = "",
    author_user_id: int | None = None,
    task_id: int | None = None,
    agent_id: int | None = None,
) -> list[Mention]:
    """A mention for each person with access named in ``text`` (not the one who wrote it)."""
    names = usernames(text)
    if not names:
        return []
    found = [u for u in await people(session, project_id) if u.username in names and u.id != author_user_id]
    if not found:
        return []
    excerpt = truncate(re.sub(r"\s+", " ", text), EXCERPT)
    mentions = [
        Mention(user_id=u.id, project_id=project_id, kind=kind, task_id=task_id, agent_id=agent_id, author=truncate(author, 100), excerpt=excerpt) for u in found
    ]
    session.add_all(mentions)
    await session.commit()
    outs = await _outs(session, mentions)
    for out in outs:
        bus.publish(project_id, "mention.created", out)
    return mentions


async def _outs(session: AsyncSession, mentions: list[Mention]) -> list[MentionOut]:
    task_ids = {m.task_id for m in mentions if m.task_id}
    project_ids = {m.project_id for m in mentions}
    tasks = {t.id: t for t in (await session.scalars(select(Task).where(Task.id.in_(task_ids)))).all()} if task_ids else {}
    projects = dict((await session.execute(select(Project.id, Project.name).where(Project.id.in_(project_ids)))).all())
    out = []
    for m in mentions:
        task = tasks.get(m.task_id) if m.task_id else None
        item = MentionOut.model_validate(m)
        item.project_name = projects.get(m.project_id, "")
        if task is not None:
            item.task_number, item.task_title = task.number, task.title
        out.append(item)
    return out


def _visible(user: User):
    """The user's mentions in projects they still have access to."""
    q = select(Mention).where(Mention.user_id == user.id)
    if not user.is_admin:
        q = q.where(Mention.project_id.in_(select(ProjectMember.project_id).where(ProjectMember.user_id == user.id)))
    return q


async def for_user(session: AsyncSession, user: User | None) -> MentionsOut:
    """The newest mentions of a person, and how many are unread."""
    if user is None:
        return MentionsOut(unread=0, items=[])
    q = _visible(user)
    unread = await session.scalar(select(func.count()).select_from(q.where(Mention.read_at.is_(None)).subquery())) or 0
    rows = list((await session.scalars(q.order_by(Mention.id.desc()).limit(PAGE))).all())
    return MentionsOut(unread=unread, items=await _outs(session, rows))


async def mark_read(session: AsyncSession, user: User | None, ids: list[int] | None = None) -> None:
    """Marks the user's mentions read: the ones named, or all."""
    if user is None:
        return
    q = update(Mention).where(Mention.user_id == user.id, Mention.read_at.is_(None))
    if ids is not None:
        q = q.where(Mention.id.in_(ids))
    await session.execute(q.values(read_at=utcnow()))
    await session.commit()


async def agent_name(session: AsyncSession, agent_id: int | None) -> str:
    return (await session.scalar(select(Agent.name).where(Agent.id == agent_id)) or "") if agent_id else ""
