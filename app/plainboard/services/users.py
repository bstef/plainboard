"""The people who sign in, and their part in projects.

An admin runs the installation: settings, subscriptions, users. Admins see every project. Everyone
else sees the projects they are a member of, with one of three roles (``models.ProjectRole``).
What a role may do is decided in one place, ``services/access.py``.

An installation without users is not set up yet: the first person to open it creates the first
admin in the browser (``setup``). On a server, ``PLAINBOARD_PASSWORD`` is the setup password that
person must know, so nobody else can claim a fresh installation.
"""

from __future__ import annotations

import asyncio
import re
import time

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_config
from ..models import Project, ProjectMember, ProjectRole, User
from ..security import hash_password, password_matches, verify_password
from .errors import NotFound, ServiceError

USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,58}$")
MIN_PASSWORD = 8
# Wrong passwords for one name: after this many within the window, further tries wait.
MAX_FAILURES, FAILURE_WINDOW_SECONDS = 8, 300
_failures: dict[str, list[float]] = {}
_NOBODY = hash_password("nobody signs in with this")


def clean_username(value: str) -> str:
    username = value.strip().lower()
    if not USERNAME.match(username):
        raise ServiceError("A user name has 2 to 60 characters: letters, digits, dot, dash or underscore.")
    return username


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise ServiceError(f"A password has at least {MIN_PASSWORD} characters.")


async def count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count(User.id))) or 0)


def setup_password_required() -> bool:
    return bool(get_config().password)


async def get(session: AsyncSession, user_id: int) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise NotFound("User not found")
    return user


async def list_all(session: AsyncSession) -> list[User]:
    return list(await session.scalars(select(User).order_by(func.lower(User.name), User.id)))


async def create(
    session: AsyncSession, username: str, name: str, password: str, *, is_admin: bool = False, may_create_projects: bool = False
) -> User:
    username = clean_username(username)
    _check_password(password)
    if await session.scalar(select(User.id).where(User.username == username)):
        raise ServiceError(f"The user name {username} is taken.")
    user = User(
        username=username,
        name=name.strip() or username,
        password_hash=await asyncio.to_thread(hash_password, password),
        is_admin=is_admin,
        may_create_projects=may_create_projects,
    )
    session.add(user)
    await session.commit()
    return user


async def setup(session: AsyncSession, username: str, name: str, password: str, setup_password: str | None) -> User:
    """The first admin of an installation that has no users yet."""
    if await count(session):
        raise ServiceError("This PlainBoard is already set up. Sign in.", 409)
    expected = get_config().password
    if expected and not password_matches(setup_password or "", expected):
        raise ServiceError("Wrong setup password (PLAINBOARD_PASSWORD of this installation).", 403)
    user = await create(session, username, name, password, is_admin=True, may_create_projects=True)
    # Two people at the same moment: only the first account stays.
    if await session.scalar(select(func.min(User.id))) != user.id:
        await session.delete(user)
        await session.commit()
        raise ServiceError("This PlainBoard is already set up. Sign in.", 409)
    return user


async def authenticate(session: AsyncSession, username: str, password: str) -> User:
    """The user for a name and password. The answer is the same for a wrong name and a wrong password."""
    username = username.strip().lower()
    now = time.monotonic()
    recent = [t for t in _failures.get(username, []) if now - t < FAILURE_WINDOW_SECONDS]
    if len(recent) >= MAX_FAILURES:
        raise ServiceError("Too many wrong passwords. Wait a few minutes.", 429)
    user = await session.scalar(select(User).where(User.username == username))
    # A hash is checked also when the name is unknown: the answer takes as long either way.
    ok = await asyncio.to_thread(verify_password, password, user.password_hash if user else _NOBODY)
    if user is None or not ok or not user.enabled:
        if len(_failures) > 5000:  # names nobody has: forget them rather than grow
            _failures.clear()
        _failures[username] = [*recent, now]
        raise ServiceError("Wrong user name or password.", 401)
    _failures.pop(username, None)
    return user


async def _keeps_an_admin(session: AsyncSession, user: User) -> None:
    others = await session.scalar(select(func.count(User.id)).where(User.is_admin, User.enabled, User.id != user.id))
    if not others:
        raise ServiceError("This is the only admin. Make someone else an admin first.")


async def update(
    session: AsyncSession,
    user: User,
    *,
    name: str | None = None,
    password: str | None = None,
    is_admin: bool | None = None,
    may_create_projects: bool | None = None,
    enabled: bool | None = None,
) -> User:
    if (is_admin is False or enabled is False) and user.is_admin and user.enabled:
        await _keeps_an_admin(session, user)
    if name is not None:
        user.name = name.strip() or user.username
    if password is not None:
        _check_password(password)
        user.password_hash = await asyncio.to_thread(hash_password, password)
        user.session_version += 1  # every browser signs in again
    if is_admin is not None:
        user.is_admin = is_admin
    if may_create_projects is not None:
        user.may_create_projects = may_create_projects
    if enabled is not None and enabled != user.enabled:
        user.enabled = enabled
        user.session_version += 1
    await session.commit()
    return user


async def change_own_password(session: AsyncSession, user: User, current: str, new: str) -> User:
    if not await asyncio.to_thread(verify_password, current, user.password_hash):
        raise ServiceError("The current password is wrong.", 403)
    return await update(session, user, password=new)


async def delete(session: AsyncSession, user: User) -> None:
    if user.is_admin and user.enabled:
        await _keeps_an_admin(session, user)
    await session.delete(user)
    await session.commit()


# --- projects --------------------------------------------------------------------------------


async def role_in(session: AsyncSession, user: User, project_id: int) -> ProjectRole | None:
    """The user's role in a project; an admin owns every project."""
    if user.is_admin:
        return ProjectRole.OWNER
    role = await session.scalar(select(ProjectMember.role).where(ProjectMember.project_id == project_id, ProjectMember.user_id == user.id))
    return ProjectRole(role) if role else None


async def roles(session: AsyncSession, user: User) -> dict[int, ProjectRole]:
    """The projects a user sees, with the role in each."""
    if user.is_admin:
        return {project_id: ProjectRole.OWNER for project_id in await session.scalars(select(Project.id))}
    rows = await session.execute(select(ProjectMember.project_id, ProjectMember.role).where(ProjectMember.user_id == user.id))
    return {project_id: ProjectRole(role) for project_id, role in rows.all()}


async def members(session: AsyncSession, project_id: int) -> list[tuple[User, ProjectRole]]:
    rows = await session.execute(
        select(User, ProjectMember.role).join(ProjectMember, ProjectMember.user_id == User.id).where(ProjectMember.project_id == project_id).order_by(func.lower(User.name))
    )
    return [(user, ProjectRole(role)) for user, role in rows.all()]


async def humans(session: AsyncSession, project_id: int) -> int:
    """How many people work in a project (members who can write, and the admins)."""
    writers = select(ProjectMember.user_id).where(ProjectMember.project_id == project_id, ProjectMember.role != ProjectRole.VIEWER)
    return int(await session.scalar(select(func.count(User.id)).where(User.enabled, User.is_admin | User.id.in_(writers))) or 0)


async def author(session: AsyncSession, user: User | None, project_id: int) -> str | None:
    """The name to put on what a person writes in a project: only where several people work, so
    agents and people can tell them apart. Alone, "the user" says it all and costs no tokens."""
    if user is None or await humans(session, project_id) < 2:
        return None
    return user.name


async def set_member(session: AsyncSession, project_id: int, user_id: int, role: ProjectRole) -> None:
    await get(session, user_id)
    member = await session.scalar(select(ProjectMember).where(ProjectMember.project_id == project_id, ProjectMember.user_id == user_id))
    if member is None:
        session.add(ProjectMember(project_id=project_id, user_id=user_id, role=role))
    else:
        member.role = role
    await session.commit()


async def remove_member(session: AsyncSession, project_id: int, user_id: int) -> None:
    member = await session.scalar(select(ProjectMember).where(ProjectMember.project_id == project_id, ProjectMember.user_id == user_id))
    if member is not None:
        await session.delete(member)
        await session.commit()
