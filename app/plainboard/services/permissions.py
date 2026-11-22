"""What a person's projects may use, set per person by an admin (Settings > Users).

A project is its creator's (``Project.owner_id``). What that person may use holds for the project,
whoever works in it and whoever changes its settings: the subscriptions its agents run on, the runners
it may be moved to, how many of its members work at once (across all of that person's projects), and
how many projects the person may create. Unset means no limit, which is what everyone had before.
A project of an admin, or of nobody (its creator was deleted): everything.

What the installation holds for itself (the global secrets, its GitHub token and SSH key, the global MCP
servers) a project gets only if its owner may use it (``User.may_use_shared``): ``shared``. Everything
that hands credentials or secrets to a project's runs, jobs and git work takes them from here.

The project's own limit to some subscriptions (``Project.account_ids``, set by an admin) comes on top.
"""

from __future__ import annotations

from typing import Any

from plainboard_protocol import Credentials
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Account, Project, Runner, Secret, User
from ..security import decrypt
from . import secrets as secret_service
from . import settings as settings_service
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


INSTALLATION_ONLY = ("github_token", "git_ssh_key")  # settings a project without ``shared`` does not get
NO_GITHUB_TOKEN = "This project has no GitHub token of its own (the installation's is not for it): add a GitHub token in the project settings."


def may_use_shared(user: User | None) -> bool:
    return not limited(user) or user.may_use_shared


async def shared(session: AsyncSession, project: Project) -> bool:
    """May the project use what the installation holds for itself (global secrets, its GitHub token and SSH key)?"""
    return may_use_shared(await owner(session, project))


async def secrets(session: AsyncSession, project: Project) -> list[Secret]:
    """The secrets the project's agents get: its own, and the global ones if it may use them."""
    return await secret_service.effective(session, project.id, shared=await shared(session, project))


async def credentials(session: AsyncSession, project: Project, values: dict[str, Any] | None = None, account: dict[str, Any] | None = None) -> Credentials:
    """Everything a run, job or git operation of the project gets: its own GitHub token (else the installation's,
    if it may use it), the secrets it may use (``Credentials.secrets``), and ``account`` (``accounts.fields``)."""
    values = values if values is not None else await settings_service.get_all(session)
    share = await shared(session, project)
    if not share:
        values = {k: v for k, v in values.items() if k not in INSTALLATION_ONLY}
    env = secret_service.env(await secret_service.effective(session, project.id, shared=share))
    return settings_service.credentials(values, decrypt(project.github_token_enc), env, account)


def github_hint(credentials: Credentials) -> str:
    """Said where git or GitHub work of a project failed that has no GitHub token to use."""
    return "" if credentials.github_token else f" {NO_GITHUB_TOKEN}"


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
