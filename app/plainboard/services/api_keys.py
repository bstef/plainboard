"""Keys for a project's read-only public API. A project can have several, each with a label
(e.g. "Website", "Status page"), so one can be revoked without breaking the others. Only the hash
is stored: a key is shown once, when it is created."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Project, PublicApiKey, utcnow
from .errors import NotFound

USED_EVERY = timedelta(hours=1)  # how often "last used" is written; the API is read on every page view


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def list_for_project(session: AsyncSession, project_id: int) -> list[PublicApiKey]:
    return list((await session.scalars(select(PublicApiKey).where(PublicApiKey.project_id == project_id).order_by(PublicApiKey.id))).all())


async def create(session: AsyncSession, project: Project, label: str = "") -> tuple[PublicApiKey, str]:
    """The stored key and its secret, which can't be read again later."""
    secret = f"pb_{secrets.token_urlsafe(32)}"
    key = PublicApiKey(project_id=project.id, label=label.strip(), key_hash=_hash(secret), hint=secret[-4:])
    session.add(key)
    await session.commit()
    return key, secret


async def get(session: AsyncSession, key_id: int) -> PublicApiKey:
    key = await session.get(PublicApiKey, key_id)
    if key is None:
        raise NotFound("API key not found")
    return key


async def rename(session: AsyncSession, key: PublicApiKey, label: str) -> PublicApiKey:
    key.label = label.strip()
    await session.commit()
    return key


async def delete(session: AsyncSession, key: PublicApiKey) -> None:
    await session.delete(key)
    await session.commit()


async def project_for(session: AsyncSession, secret: str) -> Project | None:
    """The project a key belongs to; notes now and then that the key is in use."""
    key = await session.scalar(select(PublicApiKey).where(PublicApiKey.key_hash == _hash(secret)))
    if key is None:
        return None
    now = utcnow()
    if key.last_used_at is None or now - key.last_used_at > USED_EVERY:
        key.last_used_at = now
        await session.commit()
    return await session.get(Project, key.project_id)
