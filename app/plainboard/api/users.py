from __future__ import annotations

from fastapi import APIRouter, Response

from ..schemas import MemberIn, MemberOut, PersonOut, UserCreate, UserOut, UserUpdate
from ..services import permissions
from ..services import projects as project_service
from ..services import users as user_service
from ..services.errors import NotFound
from .deps import Runs, Session

router = APIRouter(prefix="/api", tags=["users"])


@router.get("/users", response_model=list[UserOut])
async def list_users(session: Session) -> list[UserOut]:
    return [UserOut.model_validate(u) for u in await user_service.list_all(session)]


@router.get("/users/directory", response_model=list[PersonOut])
async def directory(session: Session) -> list[PersonOut]:
    """Names only, for everyone signed in: whom a project can be shared with."""
    return [PersonOut.model_validate(u) for u in await user_service.list_all(session) if u.enabled]


@router.get("/avatars/{user_id}")
async def avatar(user_id: int, session: Session) -> Response:
    """A user's profile picture. Its address carries the version, so it may be kept for good."""
    found = await user_service.avatar(session, user_id)
    if found is None:
        raise NotFound("No picture")
    data, content_type = found
    return Response(data, media_type=content_type, headers={"Cache-Control": "private, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"})


@router.post("/users", response_model=UserOut, status_code=201)
async def create_user(body: UserCreate, session: Session) -> UserOut:
    user = await user_service.create(session, body.username, body.name, body.password, is_admin=body.is_admin, may_create_projects=body.may_create_projects)
    return UserOut.model_validate(user)


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(user_id: int, body: UserUpdate, session: Session, runs: Runs) -> UserOut:
    user = await user_service.get(session, user_id)
    fields = body.model_dump(exclude_unset=True)
    limits = {key: fields.pop(key) for key in permissions.FIELDS if key in fields}
    out = UserOut.model_validate(await user_service.update(session, user, **fields, limits=limits))
    runs.recheck()  # another limit of members at work: waiting turns may start now
    return out


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(user_id: int, session: Session) -> None:
    await user_service.delete(session, await user_service.get(session, user_id))


@router.get("/projects/{project_id}/members", response_model=list[MemberOut])
async def list_members(project_id: int, session: Session) -> list[MemberOut]:
    await project_service.get(session, project_id)
    return [MemberOut(user_id=u.id, username=u.username, name=u.name, role=role) for u, role in await user_service.members(session, project_id)]


@router.put("/projects/{project_id}/members/{user_id}", response_model=list[MemberOut])
async def set_member(project_id: int, user_id: int, body: MemberIn, session: Session) -> list[MemberOut]:
    await project_service.get(session, project_id)
    await user_service.set_member(session, project_id, user_id, body.role)
    return await list_members(project_id, session)


@router.delete("/projects/{project_id}/members/{user_id}", response_model=list[MemberOut])
async def remove_member(project_id: int, user_id: int, session: Session) -> list[MemberOut]:
    await user_service.remove_member(session, project_id, user_id)
    return await list_members(project_id, session)
