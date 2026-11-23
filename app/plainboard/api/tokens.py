"""A person's own tokens for MCP clients and the API (services/personal_tokens.py). Only in the browser:
a token can't make or see tokens (access.TOKEN_HIDDEN)."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter

from ..models import utcnow
from ..schemas import PersonalTokenIn, PersonalTokenOut
from ..services import personal_tokens as token_service
from ..services.errors import ServiceError
from .deps import Session, Who

router = APIRouter(prefix="/api/tokens", tags=["tokens"])


def _user(who: Who):  # noqa: ANN202
    if who.user is None:
        raise ServiceError("Only a signed-in user has tokens.", 403)
    return who.user


@router.get("", response_model=list[PersonalTokenOut])
async def list_tokens(who: Who, session: Session) -> list[PersonalTokenOut]:
    if who.user is None:
        return []
    return [PersonalTokenOut.model_validate(t) for t in await token_service.list_for(session, who.user)]


@router.post("", response_model=PersonalTokenOut, status_code=201)
async def create_token(body: PersonalTokenIn, who: Who, session: Session) -> PersonalTokenOut:
    """The answer carries the token itself, this once."""
    expires = utcnow() + timedelta(days=body.expires_in_days) if body.expires_in_days else None
    token, secret = await token_service.create(session, _user(who), body.label, body.read_only, expires)
    out = PersonalTokenOut.model_validate(token)
    out.token = secret
    return out


@router.delete("/{token_id}", status_code=204)
async def revoke_token(token_id: int, who: Who, session: Session) -> None:
    await token_service.revoke(session, _user(who), token_id)
