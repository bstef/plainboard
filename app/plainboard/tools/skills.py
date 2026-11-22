"""Claude skills of the project for the manager: the same service, checks and limits as Project settings → Skills.

install_skill is declared local: the runner reads a file the manager names (its workspace, /uploads) and sends
its content along (``content_b64``); a git source goes through as it is.
"""

from __future__ import annotations

import base64
import binascii
from pathlib import PurePath

from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema

from ..models import Skill
from ..orchestration.runner_client import RunnerError
from ..schemas import SkillImport, SkillUpdate
from ..services import agents as agent_service
from ..services import skills as skill_service
from .registry import MANAGER, ToolContext, ToolError, tool

AGENTS = 'Who gets it: ["*"] for all agents, or agent slugs/names'


class InstallSkillArgs(BaseModel):
    url: str | None = Field(default=None, max_length=500, description="Git repository (https://, ssh:// or git@)")
    subfolder: str = Field(default="", max_length=300, description="Git: the skill's folder in the repository")
    ref: str = Field(default="", max_length=200, description="Git: branch or tag")
    file: str | None = Field(default=None, description="Or: a zip of the skill's folder or its SKILL.md (path in your workspace or /uploads)")
    agents: list[str] = Field(min_length=1, description=AGENTS)
    content_b64: SkipJsonSchema[str | None] = None  # the file's content, filled in by the runner


class SkillRef(BaseModel):
    skill: str = Field(description="Name or id")


class UpdateSkillArgs(SkillRef):
    enabled: bool | None = None
    agents: list[str] | None = Field(default=None, description=AGENTS)


async def _targets(ctx: ToolContext, refs: list[str]) -> tuple[bool, list[int]]:
    if "*" in refs:
        return True, []
    return False, list(dict.fromkeys([(await agent_service.resolve(ctx.session, ctx.project.id, r)).id for r in refs]))


async def _find(ctx: ToolContext, ref: str) -> Skill:
    text = ref.strip()
    for skill in await skill_service.list_for_project(ctx.session, ctx.project.id):
        if str(skill.id) == text or skill.name == text.lower():
            return skill
    raise ToolError(f"No skill '{ref}' (list_skills shows them).")


async def _who(ctx: ToolContext, skill: Skill) -> str:
    if skill.all_agents:
        return "all agents"
    slugs = {a.id: a.slug for a in await agent_service.list_for_project(ctx.session, ctx.project.id)}
    return ", ".join(slugs[i] for i in skill.agent_ids or [] if i in slugs) or "no agent"


@tool("list_skills", "The project's Claude skills: name, on/off, who gets them, source.", roles=(MANAGER,))
async def list_skills(ctx: ToolContext, _args) -> str:
    rows = await skill_service.list_for_project(ctx.session, ctx.project.id)
    if not rows:
        return "No skills."
    lines = []
    for s in rows:
        source = f"git {s.source_url}" + (f" {s.source_path}" if s.source_path else "") + (f" @{s.source_ref}" if s.source_ref else "") if s.source == "git" else "upload"
        lines.append(f"- [{s.id}] {s.name} ({'on' if s.enabled else 'off'}, {await _who(ctx, s)}; {source}): {s.description[:120]}")
    return "\n".join(lines)


@tool(
    "install_skill",
    "Install a Claude skill for the project from git (url) or a file, for all or chosen agents.",
    InstallSkillArgs,
    roles=(MANAGER,),
    local=True,
)
async def install_skill(ctx: ToolContext, args: InstallSkillArgs) -> str:
    if bool(args.url) == bool(args.file):
        raise ToolError("Give either url (git) or file.")
    all_agents, agent_ids = await _targets(ctx, args.agents)
    if args.url:
        body = SkillImport(url=args.url, ref=args.ref, path=args.subfolder, all_agents=all_agents, agent_ids=agent_ids)
        try:
            skill = await skill_service.import_git(ctx.session, ctx.runs.runners.of(ctx.project.id), ctx.project, body)
        except RunnerError as exc:
            raise ToolError(str(exc)) from exc
    else:
        if args.content_b64 is None:
            raise ToolError("Your runner could not read the file: give its path in your workspace or /uploads.")
        try:
            data = base64.b64decode(args.content_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ToolError("The file's content did not arrive.") from exc
        bundle = skill_service.parse_upload(PurePath(args.file or "").name, data)
        skill = await skill_service.store(ctx.session, ctx.project.id, bundle, all_agents=all_agents, agent_ids=agent_ids)
    return f"Installed skill {skill.name} [{skill.id}] for {await _who(ctx, skill)} ({skill.file_count} files). It reaches them with their next turn."


@tool("update_skill", "Turn a skill on or off, or change who gets it.", UpdateSkillArgs, roles=(MANAGER,))
async def update_skill(ctx: ToolContext, args: UpdateSkillArgs) -> str:
    skill = await _find(ctx, args.skill)
    body = SkillUpdate(enabled=args.enabled)
    if args.agents is not None:
        body.all_agents, body.agent_ids = await _targets(ctx, args.agents)
    skill = await skill_service.update(ctx.session, skill, body)
    return f"Skill {skill.name}: {'on' if skill.enabled else 'off'}, {await _who(ctx, skill)}."


@tool("remove_skill", "Remove a skill from the project.", SkillRef, roles=(MANAGER,))
async def remove_skill(ctx: ToolContext, args: SkillRef) -> str:
    skill = await _find(ctx, args.skill)
    name = skill.name
    await skill_service.delete(ctx.session, skill)
    return f"Removed skill {name}."
