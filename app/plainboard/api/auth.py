from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from ..models import User
from ..schemas import UserOut
from ..security import SESSION_COOKIE, SESSION_MAX_AGE, make_session_cookie
from ..services import users as user_service
from ..services.errors import ServiceError
from .deps import Session, Who

router = APIRouter(prefix="/api/auth", tags=["auth"])


class AuthStatus(BaseModel):
    setup_required: bool  # no user yet: the first admin is created in the browser
    setup_password_required: bool  # ... and that takes PLAINBOARD_PASSWORD
    user: UserOut | None  # who is signed in


class LoginIn(BaseModel):
    username: str
    password: str


class SetupIn(BaseModel):
    username: str
    name: str = ""
    password: str
    setup_password: str | None = None


class MeUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    current_password: str | None = None
    new_password: str | None = None


def _sign_in(response: Response, user: User) -> None:
    response.set_cookie(SESSION_COOKIE, make_session_cookie(user.id, user.session_version), max_age=SESSION_MAX_AGE, httponly=True, samesite="lax")


@router.get("/status", response_model=AuthStatus)
async def status(request: Request, session: Session) -> AuthStatus:
    who = getattr(request.state, "principal", None)
    return AuthStatus(
        setup_required=not await user_service.count(session),
        setup_password_required=user_service.setup_password_required(),
        user=UserOut.model_validate(who.user) if who and who.user else None,
    )


@router.post("/setup", response_model=UserOut, status_code=201)
async def setup(body: SetupIn, session: Session, response: Response) -> UserOut:
    """The first admin, once: an installation without users is not set up yet."""
    user = await user_service.setup(session, body.username, body.name, body.password, body.setup_password)
    _sign_in(response, user)
    return UserOut.model_validate(user)


@router.post("/login", response_model=UserOut)
async def login(body: LoginIn, session: Session, response: Response) -> UserOut:
    user = await user_service.authenticate(session, body.username, body.password)
    _sign_in(response, user)
    return UserOut.model_validate(user)


@router.post("/logout", status_code=204)
async def logout(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE)


@router.patch("/me", response_model=UserOut)
async def update_me(body: MeUpdate, who: Who, session: Session, response: Response) -> UserOut:
    """One's own name and password. A new password signs every other browser out."""
    if who.user is None:
        raise ServiceError("Only a signed-in user has an account.", 403)
    user = await user_service.get(session, who.user.id)
    if body.name is not None:
        user = await user_service.update(session, user, name=body.name)
    if body.new_password is not None:
        user = await user_service.change_own_password(session, user, body.current_password or "", body.new_password)
        _sign_in(response, user)
    return UserOut.model_validate(user)
