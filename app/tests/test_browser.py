import asyncio

import pytest
from conftest import wait_for
from starlette.websockets import WebSocketDisconnect


def test_browser_window_is_passed_on_to_the_runner_for_the_signed_in_user(client, project, monkeypatch):
    from plainboard.api import browser as browser_api

    sent, asked = [], []

    class Upstream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def send(self, message):
            sent.append(message)

        async def __aiter__(self):
            yield '{"t": "state", "url": ""}'
            yield b"\xff\xd8frame"
            await asyncio.Event().wait()

    async def connect(url, **_):
        asked.append(url)
        return Upstream()

    monkeypatch.setattr(browser_api, "connect", connect)
    with client.websocket_connect(f"/api/projects/{project['id']}/browser") as ws:
        assert ws.receive_json() == {"t": "state", "url": ""}
        assert ws.receive_bytes() == b"\xff\xd8frame"
        ws.send_text('{"t": "nav", "url": "5173"}')
        wait_for(lambda: sent == ['{"t": "nav", "url": "5173"}'])
    assert asked == [f"ws://runner/browser/project-{project['id']}"]  # one browser per project

    with pytest.raises(WebSocketDisconnect) as gone:
        with client.websocket_connect("/api/projects/999/browser"):
            pass
    assert gone.value.code == 4404

    # It is a browser inside the agents' environment: never without the login, shared previews or not.
    client.cookies.clear()
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(f"/api/projects/{project['id']}/browser"):
            pass
    assert refused.value.code == 4401 and len(asked) == 1
