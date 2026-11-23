"""A person's own tokens, for MCP clients (api/mcp_server.py) and scripts that use the API.

A token acts as the person who made it: their roles in projects, admin or not, their limits
(``access.check`` treats it like their login), optionally for reading only and until a date. Only the
hash is stored: a token is shown once, when it is created. The admin can switch tokens off for the
whole installation (setting ``mcp_enabled``): then none is accepted anywhere.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections import deque
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import McpCall, PersonalToken, User, utcnow
from . import settings as settings_service
from .errors import NotFound, ServiceError

PREFIX = "pbp_"  # PlainBoard, personal
USED_EVERY = timedelta(minutes=10)  # how often "last used" is written: a client makes many requests
MAX_PER_USER = 20
AUDIT_DAYS = 90
RATE = 120  # requests per token ...
RATE_WINDOW = 60.0  # ... in this many seconds


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def enabled(session: AsyncSession) -> bool:
    return bool(await settings_service.get(session, "mcp_enabled"))


async def list_for(session: AsyncSession, user: User) -> list[PersonalToken]:
    return list(await session.scalars(select(PersonalToken).where(PersonalToken.user_id == user.id).order_by(PersonalToken.id)))


async def create(session: AsyncSession, user: User, label: str = "", read_only: bool = False, expires_at: datetime | None = None) -> tuple[PersonalToken, str]:
    """The stored token and its secret, which can't be read again later."""
    if not await enabled(session):
        raise ServiceError("Personal tokens are switched off on this PlainBoard.", 403)
    if expires_at is not None and expires_at <= utcnow():
        raise ServiceError("The expiry date has passed already.")
    if len(await list_for(session, user)) >= MAX_PER_USER:
        raise ServiceError(f"At most {MAX_PER_USER} tokens per person: revoke one first.")
    secret = PREFIX + secrets.token_urlsafe(32)
    token = PersonalToken(user_id=user.id, label=label.strip()[:200], token_hash=_hash(secret), hint=secret[-4:], read_only=read_only, expires_at=expires_at)
    session.add(token)
    await session.commit()
    return token, secret


async def revoke(session: AsyncSession, user: User, token_id: int) -> None:
    """Only one's own: not even an admin revokes another person's token here (switching that person off does)."""
    token = await session.get(PersonalToken, token_id)
    if token is None or token.user_id != user.id:
        raise NotFound("Token not found")
    await session.delete(token)
    await session.commit()


async def authenticate(session: AsyncSession, secret: str) -> tuple[User, PersonalToken] | None:
    """The person and token of a valid secret: it exists, has not expired, its person is not switched
    off, and tokens are allowed. Notes now and then that it is in use."""
    if not secret.startswith(PREFIX):
        return None
    token = await session.scalar(select(PersonalToken).where(PersonalToken.token_hash == _hash(secret)))
    now = utcnow()
    if token is None or (token.expires_at is not None and token.expires_at <= now):
        return None
    user = await session.get(User, token.user_id)
    if user is None or not user.enabled or not await enabled(session):
        return None
    if token.last_used_at is None or now - token.last_used_at > USED_EVERY:
        token.last_used_at = now
        await session.commit()
    return user, token


class RateLimit:
    """Requests per token in a sliding window, in this process: an MCP client that runs in circles stops
    here instead of filling the board."""

    def __init__(self, rate: int = RATE, window: float = RATE_WINDOW) -> None:
        self.rate, self.window = rate, window
        self._seen: dict[int, deque[float]] = {}

    def allow(self, token_id: int) -> bool:
        now = time.monotonic()
        seen = self._seen.setdefault(token_id, deque())
        while seen and seen[0] <= now - self.window:
            seen.popleft()
        if len(seen) >= self.rate:
            return False
        seen.append(now)
        return True


async def audit(session: AsyncSession, user_id: int, token_id: int, tool: str, project_id: int | None, error: str = "") -> None:
    """One tool call in the audit (who, which tool, which project, when); older entries go after AUDIT_DAYS."""
    call = McpCall(user_id=user_id, token_id=token_id, tool=tool[:60], project_id=project_id, ok=not error, error=error[:300])
    session.add(call)
    await session.flush()
    if call.id % 200 == 0:
        await session.execute(delete(McpCall).where(McpCall.at < utcnow() - timedelta(days=AUDIT_DAYS)))
    await session.commit()

