"""Reverse proxy to web servers in the runner (see services/preview.py). Thin: HTTP and WebSocket plumbing."""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

import httpx
from fastapi import APIRouter, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.requests import HTTPConnection
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from ..config import get_config
from ..db import sessionmaker
from ..security import SESSION_COOKIE, session_cookie_valid
from ..services import preview
from ..services.errors import ServiceError

log = logging.getLogger(__name__)
router = APIRouter(include_in_schema=False)
METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


UNLOCK_PREFIX = "/api/public/preview/"  # under the public API: its visitors are not signed in


async def access(conn: HTTPConnection, port: int) -> Literal["ok", "password", "denied"]:
    """The signed-in user opens every preview; anyone else only a shared one."""
    if not get_config().password or session_cookie_valid(conn.cookies.get(SESSION_COOKIE)):
        return "ok"
    async with sessionmaker()() as session:
        row = await preview.active_share(session, port)
    if row is None:
        return "denied"
    if row.access == "public" or preview.unlocked(row, conn.cookies.get(preview.unlock_cookie(port))):
        return "ok"
    return "password"


def new_client() -> httpx.AsyncClient:
    # No read timeout: previews stream (server-sent events, long polls).
    return httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5), follow_redirects=False, limits=httpx.Limits(max_connections=200))


def redirect_from_preview_page(request: Request) -> Response | None:
    """A preview page asked for a path at the root (absolute URL in its code): send it back under its prefix."""
    path = request.url.path
    if path.startswith((preview.PREFIX + "/", UNLOCK_PREFIX)):
        return None
    port = preview.port_of_referer(request.headers.get("referer"), request.headers.get("host"))
    if port is None:
        return None
    query = request.url.query
    return RedirectResponse(preview.prefix(port) + path + (f"?{query}" if query else ""), status_code=307)


@router.api_route("/preview/{port}", methods=METHODS)
async def preview_root(port: int) -> Response:
    return RedirectResponse(f"{preview.prefix(preview.check_port(port))}/", status_code=307)


@router.api_route("/preview/{port}/{path:path}", methods=METHODS)
async def preview_http(request: Request, port: int, path: str) -> Response:
    preview.check_port(port)
    allowed = await access(request, port)
    if allowed == "password":
        return HTMLResponse(preview.unlock_page(port), status_code=401)
    if allowed == "denied":
        return PlainTextResponse("This preview is private. Sign in to PlainBoard first.", status_code=401)
    client: httpx.AsyncClient = request.app.state.preview_client
    body = await request.body()
    upstream = client.build_request(
        request.method,
        preview.target(port, path, request.url.query),
        headers=preview.request_headers(list(request.headers.items()), port),
        content=body or None,
    )
    try:
        resp = await client.send(upstream, stream=True)
    except httpx.HTTPError:
        return PlainTextResponse(preview.unreachable(port), status_code=502)
    out = StreamingResponse(resp.aiter_raw(), status_code=resp.status_code, background=BackgroundTask(resp.aclose))
    for name, value in preview.response_headers(resp.headers.multi_items(), port):
        out.headers.append(name, value)
    out.set_cookie(preview.PORT_COOKIE, str(port), samesite="lax")
    return out


@router.post(UNLOCK_PREFIX + "{port}/unlock")
async def unlock(port: int, password: str = Form(...)) -> Response:
    async with sessionmaker()() as session:
        row = await preview.active_share(session, port)
    if row is None or row.access != "password" or not preview.password_ok(row, password):
        await asyncio.sleep(1)  # slows down guessing
        return HTMLResponse(preview.unlock_page(port, wrong=True), status_code=401)
    out = RedirectResponse(f"{preview.prefix(port)}/", status_code=303)
    out.set_cookie(preview.unlock_cookie(port), preview.unlock_value(row), max_age=preview.UNLOCK_MAX_AGE, httponly=True, samesite="lax")
    return out


@router.websocket("/preview/{port}/{path:path}")
async def preview_ws(websocket: WebSocket, port: int, path: str) -> None:
    await _pipe(websocket, port, path)


@router.websocket("/{path:path}")
async def preview_ws_at_root(websocket: WebSocket, path: str) -> None:
    """Hot reload sockets go to the root and carry no hint but the cookie of the last preview."""
    port = preview.port_of_cookie(websocket.cookies.get(preview.PORT_COOKIE))
    if port is None or path.startswith("api/"):
        await websocket.close(code=4404)
        return
    await _pipe(websocket, port, path)


async def _pipe(websocket: WebSocket, port: int, path: str) -> None:
    try:
        preview.check_port(port)
    except ServiceError:
        await websocket.close(code=4404)
        return
    if await access(websocket, port) != "ok":
        await websocket.close(code=4401)
        return
    headers = [(k, v) for k, v in preview.request_headers(list(websocket.headers.items()), port) if k.lower() in ("cookie", "user-agent", "accept-language")]
    try:
        upstream = await connect(
            f"ws://localhost:{port}/{path}" + (f"?{websocket.url.query}" if websocket.url.query else ""),
            host=preview.runner_host(),  # where to connect; the URL is what the server sees
            port=port,
            origin=f"http://localhost:{port}",
            subprotocols=websocket.scope.get("subprotocols") or None,
            additional_headers=headers,
            open_timeout=5,
            max_size=None,
        )
    except (OSError, TimeoutError, InvalidHandshake):
        await websocket.close(code=1013)
        return
    async with upstream:
        await websocket.accept(subprotocol=upstream.subprotocol)

        async def to_server() -> None:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                await upstream.send(message["text"] if message.get("text") is not None else message["bytes"])

        async def to_browser() -> None:
            async for message in upstream:
                if isinstance(message, str):
                    await websocket.send_text(message)
                else:
                    await websocket.send_bytes(message)

        tasks = [asyncio.create_task(to_server()), asyncio.create_task(to_browser())]
        try:
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if (exc := task.exception()) and not isinstance(exc, (ConnectionClosed, WebSocketDisconnect, RuntimeError)):
                    log.info("Preview socket on port %s ended: %s", port, exc)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await websocket.close()
            except RuntimeError:
                pass  # the browser closed first
