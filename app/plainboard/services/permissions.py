"""What a person's projects may use, set per person by an admin (Settings > Users).

A project is its creator's (``Project.owner_id``). What that person may use holds for the project,
whoever works in it and whoever changes its settings: the subscriptions its agents run on, the runners
it may be moved to, how many of its members work at once (across all of that person's projects), and
how many projects the person may create. Unset means no limit, which is what everyone had before.
A project of an admin, or of nobody (its creator was deleted): everything.

The project's own limit to some subscriptions (``Project.account_ids``, set by an admin) comes on top.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Account, Project, Runner, User
from .errors import NotFound, ServiceError

BUILTIN = 0  # the built-in runner in ``User.runner_ids``
FIELDS = ("max_projects", "account_ids", "runner_ids", "max_parallel")


def limited(user: User | None) -> bool:
    return user is not None and not user.is_admin


async def owner(session: AsyncSession, project: Project) -> User | None:
    """The person whose limits hold for the project; None: nobody's (an admin's): everything."""
    user = await session.get(User, project.owner_id) if project.owner_id else None
    return user if limited(user) else None


async def account_ids(session: AsyncSession, project: Project | None) -> set[int] | None:
    """The only accounts the project's agents may run on; None: any."""
    if project is None:
        return None
    allowed = set(project.account_ids) if project.account_ids else None
    user = await owner(session, project)
    if user is not None and user.account_ids is not None:
        allowed = set(user.account_ids) if allowed is None else allowed & set(user.account_ids)
    return allowed


async def check_account(session: AsyncSession, project: Project | None, account_id: int | None, user: User | None = None) -> None:
    """``user``: the owner of a project that is not created yet."""
    if account_id is None:
        return
    allowed = await account_ids(session, project) if project is not None else (set(user.account_ids) if limited(user) and user.account_ids is not None else None)
    if allowed is not None and account_id not in allowed:
        raise ServiceError("This project may not use that subscription.")


def may_run_on(user: User | None, runner_id: int | None) -> bool:
    return not limited(user) or user.runner_ids is None or (runner_id or BUILTIN) in user.runner_ids


async def check_runner(session: AsyncSession, project: Project, runner_id: int | None) -> None:
    if not may_run_on(await owner(session, project), runner_id):
        raise ServiceError("This project may not run on that runner.", 403)


async def first_runner(session: AsyncSession, user: User | None) -> int | None:
    """Where a new project of this person starts: the built-in runner, unless only others are allowed."""
    if may_run_on(user, None):
        return None
    ids = [i for i in user.runner_ids or [] if i != BUILTIN]
    found = await session.scalar(select(func.min(Runner.id)).where(Runner.id.in_(ids))) if ids else None
    if found is None:
        raise ServiceError("No runner is allowed for your projects. Ask an admin.", 403)
    return found


async def start_runner(session: AsyncSession, user: User | None, runner_id: int | None, chosen: bool) -> int | None:
    """The runner a new project starts on: the one chosen (None: the built-in one), checked, else ``first_runner``."""
    if not chosen:
        return await first_runner(session, user)
    if runner_id is not None and await session.get(Runner, runner_id) is None:
        raise NotFound("Runner not found")
    if not may_run_on(user, runner_id):
        raise ServiceError("Your projects may not run on that runner.", 403)
    return runner_id


async def check_create(session: AsyncSession, user: User) -> None:
    """Before anything of a new project exists: may this person have one more?"""
    if not limited(user):
        return
    if not user.may_create_projects:
        raise ServiceError("You may not create projects here. Ask an admin.", 403)
    await first_runner(session, user)
    if user.max_projects is not None:
        own = int(await session.scalar(select(func.count(Project.id)).where(Project.owner_id == user.id)) or 0)
        if own >= user.max_projects:
            raise ServiceError(f"You have {own} projects, as many as you may have. Ask an admin for more, or delete one.", 403)


async def max_parallel(session: AsyncSession, project: Project) -> int | None:
    user = await owner(session, project)
    return user.max_parallel if user is not None else None


async def caps(session: AsyncSession) -> tuple[dict[int, int], dict[int, int]]:
    """For the dispatcher: project -> its owner, for owners with a limit of members at work; owner -> that limit."""
    rows = (await session.execute(select(Project.id, User.id, User.max_parallel).join(User, User.id == Project.owner_id).where(User.max_parallel.is_not(None), ~User.is_admin))).all()
    return {pid: uid for pid, uid, _ in rows}, {uid: limit for _, uid, limit in rows}


async def clean(session: AsyncSession, values: dict) -> dict:
    """An admin's change of a person's limits (``FIELDS``), checked: the subscriptions and runners named exist."""
    out = {key: values[key] for key in FIELDS if key in values}
    for key, table, extra in (("account_ids", Account, set()), ("runner_ids", Runner, {BUILTIN})):
        if out.get(key) is None:
            continue
        ids = list(dict.fromkeys(out[key]))
        known = set(await session.scalars(select(table.id).where(table.id.in_(ids)))) | extra
        if unknown := [i for i in ids if i not in known]:
            raise NotFound(f"Not found: {unknown[0]}")
        out[key] = ids
    return out
