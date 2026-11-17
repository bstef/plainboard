"""Keys that may read this PlainBoard through its API and change nothing.

For a tool or a person that looks at how the agents work: chats, runs, tickets, the work log.
Such a key reads what an admin reads, with GET requests only (``services/access.check``), and never
what is about the credentials themselves. Only the hash is stored: a key is shown once, when it is
created.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AccessKey, utcnow
from .errors import NotFound

PREFIX = "pbr_"  # PlainBoard, read
USED_EVERY = timedelta(minutes=10)  # how often "last used" is written: a reader makes many requests


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def list_all(session: AsyncSession) -> list[AccessKey]:
    return list(await session.scalars(select(AccessKey).order_by(AccessKey.id)))


async def create(session: AsyncSession, label: str = "") -> tuple[AccessKey, str]:
    """The stored key and its secret, which can't be read again later."""
    secret = PREFIX + secrets.token_urlsafe(32)
    key = AccessKey(label=label.strip(), key_hash=_hash(secret), hint=secret[-4:])
    session.add(key)
    await session.commit()
    return key, secret


async def delete(session: AsyncSession, key_id: int) -> None:
    key = await session.get(AccessKey, key_id)
    if key is None:
        raise NotFound("Access key not found")
    await session.delete(key)
    await session.commit()


async def valid(session: AsyncSession, secret: str) -> bool:
    """Whether this is a key that exists; notes now and then that it is in use."""
    key = await session.scalar(select(AccessKey).where(AccessKey.key_hash == _hash(secret))) if secret.startswith(PREFIX) else None
    if key is None:
        return False
    now = utcnow()
    if key.last_used_at is None or now - key.last_used_at > USED_EVERY:
        key.last_used_at = now
        await session.commit()
    return True
