"""Passes a browser's WebSocket on to a socket in the runner, both ways, until one side ends."""

from __future__ import annotations

import asyncio
import logging

from fastapi import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

log = logging.getLogger(__name__)


async def relay(websocket: WebSocket, upstream: ClientConnection, what: str) -> None:
    """``websocket`` is accepted already. ``what`` names the pair in the log."""

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
                log.info("%s ended: %s", what, exc)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await websocket.close()
        except RuntimeError:
            pass  # the browser closed first
