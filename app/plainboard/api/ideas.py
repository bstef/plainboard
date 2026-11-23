"""A project's ideas: collect, sort, plan tickets from them (services/ideas.py, orchestration/ideas.py).
Members change them, viewers read (services/access.py)."""

from __future__ import annotations

from fastapi import APIRouter

from ..schemas import IdeaCreate, IdeaOut, IdeaPlanApplied, IdeaPlanApply, IdeaPlanIn, IdeaPlanOut, IdeasOut, IdeaTicketIn, IdeaTopicIn, IdeaTopicOut, IdeaUpdate
from ..services import board as board_service
from ..services import ideas as idea_service
from ..services import projects as project_service
from .deps import Runs, Session, Who

router = APIRouter(prefix="/api/projects/{project_id}/ideas", tags=["ideas"])


@router.get("", response_model=IdeasOut)
async def list_ideas(project_id: int, session: Session, runs: Runs, closed: bool = False) -> IdeasOut:
    """The open ideas and their topics; ``closed``: also the ones taken into tickets or left out."""
    await project_service.get(session, project_id)
    topics = [IdeaTopicOut.model_validate(t) for t in await idea_service.topics(session, project_id)]
    ideas = await idea_service.outs(session, await idea_service.listed(session, project_id, closed=closed))
    return IdeasOut(topics=topics, ideas=ideas, sorting=runs.ideas.sorting(project_id))


@router.post("", response_model=IdeaOut, status_code=201)
async def create_idea(project_id: int, body: IdeaCreate, session: Session, runs: Runs, who: Who) -> IdeaOut:
    await project_service.get(session, project_id)
    idea = await idea_service.create(session, project_id, body, who.name)
    if idea.sort_state == idea_service.PENDING:
        runs.ideas.sort_soon(project_id)
    (out,) = await idea_service.outs(session, [idea])
    return out


@router.post("/sort", status_code=202)
async def sort_again(project_id: int, session: Session, runs: Runs) -> dict:
    """Sort all open ideas anew (topics may be renamed, merged, dropped)."""
    await project_service.get(session, project_id)
    count = await idea_service.mark_all_pending(session, project_id)
    if count:
        runs.ideas.sort_soon(project_id, full=True)
    return {"ideas": count}


@router.post("/topics", response_model=IdeaTopicOut, status_code=201)
async def create_topic(project_id: int, body: IdeaTopicIn, session: Session) -> IdeaTopicOut:
    await project_service.get(session, project_id)
    return IdeaTopicOut.model_validate(await idea_service.create_topic(session, project_id, body))


@router.patch("/topics/{topic_id}", response_model=IdeaTopicOut)
async def update_topic(project_id: int, topic_id: int, body: IdeaTopicIn, session: Session) -> IdeaTopicOut:
    topic = await idea_service.get_topic(session, project_id, topic_id)
    return IdeaTopicOut.model_validate(await idea_service.update_topic(session, topic, body))


@router.delete("/topics/{topic_id}", status_code=204)
async def delete_topic(project_id: int, topic_id: int, session: Session) -> None:
    await idea_service.remove_topic(session, await idea_service.get_topic(session, project_id, topic_id))


@router.post("/plans", response_model=IdeaPlanOut, status_code=202)
async def plan(project_id: int, body: IdeaPlanIn, session: Session, runs: Runs) -> IdeaPlanOut:
    """Start a proposal of tickets from these ideas; it comes as the event ``idea_plan.updated`` (or GET)."""
    project = await project_service.get(session, project_id)
    return (await runs.ideas.start_plan(session, project, body.idea_ids, body.topic_id)).out()


@router.get("/plans", response_model=list[IdeaPlanOut])
async def list_plans(project_id: int, runs: Runs) -> list[IdeaPlanOut]:
    return [p.out() for p in runs.ideas.plans(project_id)]


@router.get("/plans/{plan_id}", response_model=IdeaPlanOut)
async def get_plan(project_id: int, plan_id: str, runs: Runs) -> IdeaPlanOut:
    return runs.ideas.plan(project_id, plan_id).out()


@router.post("/plans/apply", response_model=IdeaPlanApplied, status_code=201)
async def apply_plan(project_id: int, body: IdeaPlanApply, session: Session, runs: Runs) -> IdeaPlanApplied:
    """Create the tickets of the confirmed proposal and mark its ideas as taken."""
    project = await project_service.get(session, project_id)
    tasks, ideas = await idea_service.apply_plan(session, project, body)
    runs.ideas.forget(project_id, body.plan_id)
    return IdeaPlanApplied(tickets=await board_service.task_outs(session, tasks), ideas=await idea_service.outs(session, ideas))


@router.patch("/{idea_id}", response_model=IdeaOut)
async def update_idea(project_id: int, idea_id: int, body: IdeaUpdate, session: Session, runs: Runs) -> IdeaOut:
    idea = await idea_service.get(session, project_id, idea_id)
    idea, resort = await idea_service.update(session, idea, body.model_dump(exclude_unset=True))
    if resort:
        runs.ideas.sort_soon(project_id)
    (out,) = await idea_service.outs(session, [idea])
    return out


@router.delete("/{idea_id}", status_code=204)
async def delete_idea(project_id: int, idea_id: int, session: Session) -> None:
    await idea_service.remove(session, await idea_service.get(session, project_id, idea_id))


@router.post("/{idea_id}/ticket", response_model=IdeaPlanApplied, status_code=201)
async def idea_to_ticket(project_id: int, idea_id: int, body: IdeaTicketIn, session: Session) -> IdeaPlanApplied:
    """One idea straight into a ticket."""
    idea = await idea_service.get(session, project_id, idea_id)
    task = await idea_service.take_one(session, idea, body)
    return IdeaPlanApplied(tickets=await board_service.task_outs(session, [task]), ideas=await idea_service.outs(session, [idea]))
