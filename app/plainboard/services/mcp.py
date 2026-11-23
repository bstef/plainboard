"""External MCP server configuration (global or per project; all or selected agents). The global ones only for
projects that may use what the installation holds for itself (``permissions.shared``): their env and headers carry its keys."""

from __future__ import annotations

import json

from plainboard_protocol import McpServerSpec
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..events import bus
from ..models import Agent, McpServer, Project, agent_mcp_servers
from ..schemas import McpServerCreate, McpServerOut, McpServerUpdate
from ..security import decrypt, encrypt
from . import permissions
from .errors import NotFound, ServiceError

MASK = "***"
RESERVED_NAMES = {"plainboard"}


def _load(blob: str | None) -> dict[str, str]:
    raw = decrypt(blob)
    return json.loads(raw) if raw else {}


def _store(values: dict[str, str]) -> str | None:
    return encrypt(json.dumps(values)) if values else None


def _merge_secrets(new: dict[str, str], old: dict[str, str]) -> dict[str, str]:
    return {k: (old.get(k, "") if v == MASK else v) for k, v in new.items()}


def _validate(server: McpServer) -> None:
    if server.name in RESERVED_NAMES:
        raise ServiceError(f"'{server.name}' is reserved")
    if server.transport == "stdio" and not server.command:
        raise ServiceError("stdio servers need a command")
    if server.transport in ("http", "sse") and not server.url:
        raise ServiceError(f"{server.transport} servers need a url")


async def _check_unique(session: AsyncSession, project_id: int | None, name: str, exclude_id: int | None = None) -> None:
    q = select(McpServer.id).where(McpServer.name == name, or_(McpServer.project_id == project_id, McpServer.project_id.is_(None)))
    if project_id is None:
        q = select(McpServer.id).where(McpServer.name == name)
    if exclude_id:
        q = q.where(McpServer.id != exclude_id)
    if await session.scalar(q) is not None:
        raise ServiceError(f"An MCP server named '{name}' already exists")


async def _shared(session: AsyncSession, project_id: int) -> bool:
    project = await session.get(Project, project_id)
    return project is not None and await permissions.shared(session, project)


async def create(session: AsyncSession, project_id: int, data: McpServerCreate) -> McpServer:
    scope = None if data.global_scope else project_id
    if scope is None and not await _shared(session, project_id):
        raise ServiceError("This project may not add MCP servers for every project.", 403)
    await _check_unique(session, scope, data.name)
    server = McpServer(
        project_id=scope,
        name=data.name,
        transport=data.transport,
        command=data.command,
        args=data.args,
        url=data.url,
        env_enc=_store(data.env),
        headers_enc=_store(data.headers),
        enabled=data.enabled,
        all_agents=data.all_agents,
    )
    _validate(server)
    session.add(server)
    await session.commit()
    await _publish(session, server, project_id)
    return server


TARGET = ("transport", "command", "args", "url")  # what runs, or where it connects


def _keeps_secrets(server: McpServer, fields: dict) -> bool:
    """Would stored env or header values go along to another command or address with this change?"""
    if not any(key in fields and fields[key] != getattr(server, key) for key in TARGET):
        return False
    return any(
        blob and (fields.get(key) is None or MASK in fields[key].values())
        for key, blob in (("env", server.env_enc), ("headers", server.headers_enc))
    )


async def update(session: AsyncSession, server: McpServer, data: McpServerUpdate, project_id: int, *, keep_secrets: bool = True) -> McpServer:
    """``keep_secrets``: may stored values go along to another command or address (for a project's server its
    owner or an admin, for an installation's an admin)? Else such a change needs them entered again."""
    fields = data.model_dump(exclude_unset=True)
    if not keep_secrets and _keeps_secrets(server, fields):
        raise ServiceError("Only the project's owner or an admin can point this MCP server at another command or address while it keeps its stored values. Enter them again along with it.", 403)
    if "name" in fields:
        await _check_unique(session, server.project_id, fields["name"], exclude_id=server.id)
    if "env" in fields:
        server.env_enc = _store(_merge_secrets(fields.pop("env") or {}, _load(server.env_enc)))
    if "headers" in fields:
        server.headers_enc = _store(_merge_secrets(fields.pop("headers") or {}, _load(server.headers_enc)))
    for key, value in fields.items():
        setattr(server, key, value)
    _validate(server)
    await session.commit()
    await _publish(session, server, project_id)
    return server


async def delete(session: AsyncSession, server: McpServer, project_id: int) -> None:
    server_id = server.id
    await session.delete(server)
    await session.commit()
    bus.publish(project_id, "mcp.deleted", {"id": server_id})


async def get(session: AsyncSession, server_id: int, project_id: int) -> McpServer:
    server = await session.get(McpServer, server_id)
    if server is None or server.project_id not in (None, project_id) or (server.project_id is None and not await _shared(session, project_id)):
        raise NotFound(f"MCP server {server_id} not found")
    return server


async def resolve(session: AsyncSession, project_id: int, ref: str | int) -> McpServer:
    for server in await list_for_project(session, project_id):
        if str(server.id) == str(ref) or server.name == str(ref):
            return server
    raise NotFound(f"MCP server '{ref}' not found")


async def list_for_project(session: AsyncSession, project_id: int) -> list[McpServer]:
    scope = or_(McpServer.project_id == project_id, McpServer.project_id.is_(None)) if await _shared(session, project_id) else McpServer.project_id == project_id
    q = select(McpServer).where(scope).order_by(McpServer.id)
    return list((await session.scalars(q)).all())


async def to_out(session: AsyncSession, servers: list[McpServer]) -> list[McpServerOut]:
    ids = [s.id for s in servers]
    links: dict[int, list[int]] = {}
    if ids:
        for agent_id, server_id in (
            await session.execute(select(agent_mcp_servers.c.agent_id, agent_mcp_servers.c.mcp_server_id).where(agent_mcp_servers.c.mcp_server_id.in_(ids)))
        ).all():
            links.setdefault(server_id, []).append(agent_id)
    out = []
    for server in servers:
        item = McpServerOut.model_validate(server)
        item.env = {k: MASK for k in _load(server.env_enc)}
        item.headers = {k: MASK for k in _load(server.headers_enc)}
        item.agent_ids = sorted(links.get(server.id, []))
        out.append(item)
    return out


async def _publish(session: AsyncSession, server: McpServer, project_id: int) -> None:
    (out,) = await to_out(session, [server])
    bus.publish(project_id, "mcp.upserted", out)


async def specs_for_agent(session: AsyncSession, agent: Agent) -> list[McpServerSpec]:
    selected = {s.id for s in agent.mcp_servers}
    specs = []
    for server in await list_for_project(session, agent.project_id):
        if not server.enabled or not (server.all_agents or server.id in selected):
            continue
        specs.append(
            McpServerSpec(
                name=server.name,
                transport=server.transport,  # type: ignore[arg-type]
                command=server.command,
                args=server.args or [],
                env=_load(server.env_enc),
                url=server.url,
                headers=_load(server.headers_enc),
            )
        )
    return specs
