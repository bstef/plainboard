"""Sources of a project's searchable history: one indexer per kind.

An indexer reads what changed in its source since its cursor (a stamp and an id, so nothing is read
twice and nothing is skipped), in order, and turns each item into a ``Doc``. It also names the items
that still exist, so deleted ones leave the index. Add a source: subclass ``Indexer`` (or, for a
source outside the database, do as ``commit_docs`` does) and list it in ``INDEXERS``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

from plainboard_protocol import GitCommit
from sqlalchemy import Select, String, and_, cast, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ...models import Agent, Assignment, Comment, Decision, Note, Repository, SearchDocument, Task, TicketState, utcnow
from .documents import Doc


class Indexer(ABC):
    kind: str

    @abstractmethod
    def query(self, project_id: int) -> tuple[Select, Any, Any]:
        """The source's rows of a project, its stamp (when a row last changed) and its id column."""

    @abstractmethod
    def doc(self, row: Any) -> Doc | None:
        """The row as a document; None: nothing to find in it (it leaves the index)."""

    @abstractmethod
    def existing(self, project_id: int) -> Select:
        """The source ids (as text) of the items that are still there."""

    async def changed(self, s: AsyncSession, project_id: int, cursor: str, limit: int) -> list[tuple[str, str, Doc | None]]:
        """(cursor after it, source id, document) of the rows changed after ``cursor``, oldest first."""
        query, stamp, id_ = self.query(project_id)
        if cursor:
            when, _, last = cursor.partition("|")
            query = query.where(tuple_(stamp, id_) > tuple_(datetime.fromisoformat(when), int(last or 0)))
        rows = (await s.execute(query.add_columns(stamp.label("stamp"), id_.label("row_id")).order_by(stamp, id_).limit(limit))).all()
        out = []
        for row in rows:
            doc = self.doc(row)
            if doc is not None:
                doc.changed = row.stamp  # when it last changed, as the age of a hit says
            out.append((f"{row.stamp.isoformat()}|{row.row_id}", str(row.row_id), doc))
        return out


def _facts(*facts: str | None) -> list[str]:
    return [f for f in facts if f]


class Tickets(Indexer):
    kind = "ticket"

    def query(self, project_id: int):
        stamp = func.greatest(Task.updated_at, func.coalesce(TicketState.updated_at, Task.updated_at))
        query = select(Task, TicketState.text.label("state")).outerjoin(TicketState, TicketState.task_id == Task.id).where(Task.project_id == project_id)
        return query, stamp, Task.id

    def doc(self, row: Any) -> Doc | None:
        task: Task = row.Task
        body = task.description or ""
        if (row.state or "").strip():
            body += f"\n\nWhere it stands:\n{row.state.strip()}"
        labels = ", ".join(task.labels or [])
        return Doc(
            kind=self.kind, source_id=str(task.id), ref=f"#{task.number}", title=task.title, body=body, at=task.created_at,
            facts=_facts(task.status, f"{task.priority} priority", f"labels: {labels}" if labels else None, "archived" if task.archived_at else None),
            ticket_number=task.number,
        )  # fmt: skip

    def existing(self, project_id: int) -> Select:
        return select(cast(Task.id, String)).where(Task.project_id == project_id)


class Reports(Indexer):
    kind = "report"
    _done = and_(Assignment.result.is_not(None), Assignment.finished_at.is_not(None))

    def query(self, project_id: int):
        query = (
            select(Assignment, Agent.name.label("agent"), Task.number.label("number"))
            .join(Agent, Agent.id == Assignment.agent_id)
            .outerjoin(Task, Task.id == Assignment.task_id)
            .where(Assignment.project_id == project_id, self._done)
        )
        return query, Assignment.updated_at, Assignment.id

    def doc(self, row: Any) -> Doc | None:
        a: Assignment = row.Assignment
        return Doc(
            kind=self.kind, source_id=str(a.id), ref=f"assignment #{a.id}", title=a.title, body=a.result or "", at=a.finished_at or a.created_at,
            facts=_facts(f"ticket #{row.number}" if row.number else None, f"by {row.agent}", a.status), ticket_number=row.number,
        )  # fmt: skip

    def existing(self, project_id: int) -> Select:
        return select(cast(Assignment.id, String)).where(Assignment.project_id == project_id, self._done)


class Decisions(Indexer):
    kind = "decision"

    def query(self, project_id: int):
        query = select(Decision, Task.number.label("number")).join(Task, Task.id == Decision.task_id).where(Decision.project_id == project_id)
        return query, Decision.updated_at, Decision.id

    def doc(self, row: Any) -> Doc | None:
        d: Decision = row.Decision
        parts = [d.details or ""]
        options = [f"{i + 1}) {o.get('label', '')}" + (f": {o['description']}" if o.get("description") else "") for i, o in enumerate(d.options or [])]
        if options:
            parts.append("Options:\n" + "\n".join(options))
        if d.answer_option is not None and 0 <= d.answer_option < len(d.options or []):
            parts.append(f"Answer: {d.options[d.answer_option].get('label', '')}")
        if d.answer_text:
            parts.append(f"Answer: {d.answer_text}" if d.answer_option is None else d.answer_text)
        return Doc(
            kind=self.kind, source_id=str(d.id), ref=f"decision on #{row.number}", title=d.question, body="\n\n".join(p for p in parts if p),
            at=d.created_at, facts=_facts(d.status), ticket_number=row.number,
        )  # fmt: skip

    def existing(self, project_id: int) -> Select:
        return select(cast(Decision.id, String)).where(Decision.project_id == project_id)


class Comments(Indexer):
    kind = "comment"

    def query(self, project_id: int):
        author = aliased(Agent)
        query = (
            select(Comment, Task.number.label("number"), Task.title.label("ticket_title"), author.name.label("agent"))
            .join(Task, Task.id == Comment.task_id)
            .outerjoin(author, author.id == Comment.author_agent_id)
            .where(Task.project_id == project_id)
        )
        return query, Comment.created_at, Comment.id

    def doc(self, row: Any) -> Doc | None:
        c: Comment = row.Comment
        who = row.agent or c.author_name or c.author_kind
        return Doc(
            kind=self.kind, source_id=str(c.id), ref=f"comment on #{row.number}", title=row.ticket_title, body=c.body, at=c.created_at,
            facts=_facts(f"by {who}"), ticket_number=row.number,
        )  # fmt: skip

    def existing(self, project_id: int) -> Select:
        return select(cast(Comment.id, String)).join(Task, Task.id == Comment.task_id).where(Task.project_id == project_id)


class Notes(Indexer):
    kind = "note"

    def query(self, project_id: int):
        return select(Note).where(Note.project_id == project_id), Note.updated_at, Note.id

    def doc(self, row: Any) -> Doc | None:
        n: Note = row.Note
        return Doc(kind=self.kind, source_id=str(n.id), ref=f"note {n.id}", title=n.title, body=n.content, at=n.created_at, facts=_facts("pinned" if n.pinned else None))

    def existing(self, project_id: int) -> Select:
        return select(cast(Note.id, String)).where(Note.project_id == project_id)


INDEXERS: tuple[Indexer, ...] = (Tickets(), Reports(), Decisions(), Comments(), Notes())
COMMIT = "commit"
KINDS = tuple(i.kind for i in INDEXERS) + (COMMIT,)


def commit_docs(repo: Repository, branch: str, commits: list[GitCommit]) -> list[Doc]:
    """Commits of a repository's base branch: the message, the paths it changed and how much."""
    docs = []
    for c in commits:
        subject, _, rest = c.message.partition("\n")
        files = ", ".join(c.files) + (f" and {c.file_count - len(c.files)} more" if c.file_count > len(c.files) else "")
        body = rest.strip()
        if files:
            body += f"\n\nChanged {c.file_count} file{'s' if c.file_count != 1 else ''} (+{c.insertions} -{c.deletions}): {files}"
        docs.append(Doc(kind=COMMIT, source_id=f"{repo.id}:{c.sha}", ref=c.sha[:10], title=subject.strip(), body=body.strip(), at=_utc(c.date), facts=_facts(f"{repo.name} ({branch})", c.author), repo=repo.name, paths=c.files or None))
    return docs


def _utc(iso: str) -> datetime:
    try:
        at = datetime.fromisoformat(iso)
    except ValueError:
        return utcnow()
    return at.astimezone(UTC).replace(tzinfo=None) if at.tzinfo else at


def stale_commits(project_id: int) -> Any:
    """Commits of repositories the project no longer has."""
    names = select(Repository.name).where(Repository.project_id == project_id)
    return and_(SearchDocument.project_id == project_id, SearchDocument.kind == COMMIT, or_(SearchDocument.repo.is_(None), SearchDocument.repo.not_in(names)))
