from __future__ import annotations

from pydantic import BaseModel, Field

from ..schemas import ProjectUpdate
from ..services import projects as project_service
from ..services import repositories as repo_service
from ..services import settings as settings_service
from .registry import MANAGER, ToolContext, tool


class UpdateProjectArgs(BaseModel):
    name: str | None = None
    description: str | None = None
    workspace_path: str | None = None
    setup_script: str | None = Field(default=None, description="Full replacement. Must be safe to run repeatedly")
    auto_wake_manager: bool | None = None


@tool("get_project", "Project settings (name, description, workspace, repositories, setup script).", roles=(MANAGER,))
async def get_project(ctx: ToolContext, _args) -> str:
    p = ctx.project
    workspace = f"shared folder '{p.workspace_path}'" if p.workspace_path else "one directory per agent"
    repos = ", ".join(r.name for r in await repo_service.list_for_project(ctx.session, p.id)) or "-"
    values = await settings_service.get_all(ctx.session)
    limit = int(values["manager_parallel_limit"])
    parallel = f"{values['max_parallel_runs']} ({f'you may set 1-{limit}' if limit >= 1 else 'only the user can change it'}; all projects share it)"
    return (
        f"Name: {p.name}\nRepositories: {repos}\nWorkspace: {workspace}\nParallel agents: {parallel}\n"
        f"Auto-wake manager: {p.auto_wake_manager}\nDescription:\n{p.description or '-'}\nSetup script:\n{p.setup_script or '-'}"
    )


@tool("update_project", "Update project name, description, shared workspace folder, setup script or auto-wake.", UpdateProjectArgs, roles=(MANAGER,))
async def update_project(ctx: ToolContext, args: UpdateProjectArgs) -> str:
    await project_service.update(ctx.session, ctx.project, ProjectUpdate(**args.model_dump(exclude_unset=True)))
    return "Project updated."


class ParallelArgs(BaseModel):
    count: int


@tool(
    "set_parallel_agents",
    "How many agents work at the same time, across all projects. More finishes sooner but uses the token quota faster. Range: see get_project.",
    ParallelArgs,
    roles=(MANAGER,),
)
async def set_parallel_agents(ctx: ToolContext, args: ParallelArgs) -> str:
    await settings_service.set_parallel_by_manager(ctx.session, args.count)
    ctx.runs.recheck()
    return f"Up to {args.count} agents now work at the same time."
