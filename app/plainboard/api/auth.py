from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from ..config import get_config
from ..db import sessionmaker
from ..security import SESSION_COOKIE, SESSION_MAX_AGE, make_session_cookie, password_matches, session_cookie_valid
from ..services import access_keys as access_key_service

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    password: str


def is_authenticated(request: Request) -> bool:
    if not get_config().password:
        return True
    return session_cookie_valid(request.cookies.get(SESSION_COOKIE))


# What a read key never gets, although it is a GET: the keys themselves, the provider accounts and
# their sign-in, and what is fetched with the user's GitHub token.
READ_KEY_HIDDEN = ("/api/access-keys", "/api/accounts", "/api/github/")


async def read_key_refusal(request: Request) -> tuple[int, str] | None:
    """A request without the login: None if it carries a read key and only reads, else why not."""
    scheme, _, secret = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not secret.strip():
        return 401, "Not authenticated"
    async with sessionmaker()() as session:
        if not await access_key_service.valid(session, secret.strip()):
            return 401, "Not authenticated"
    if request.method not in ("GET", "HEAD") or request.url.path.startswith(READ_KEY_HIDDEN):
        return 403, "This key can only read"
    return None


@router.get("/status")
async def status(request: Request) -> dict[str, bool]:
    return {"password_required": bool(get_config().password), "authenticated": is_authenticated(request)}


@router.post("/login")
async def login(body: LoginIn, response: Response) -> dict[str, bool]:
    expected = get_config().password
    if not expected:
        return {"authenticated": True}
    if not password_matches(body.password, expected):
        raise HTTPException(401, "Wrong password")
    response.set_cookie(SESSION_COOKIE, make_session_cookie(), max_age=SESSION_MAX_AGE, httponly=True, samesite="lax")
    return {"authenticated": True}


@router.post("/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(SESSION_COOKIE)
    return {"authenticated": False}
