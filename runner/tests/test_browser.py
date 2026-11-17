import asyncio

import pytest
from plainboard_runner import browser
from plainboard_runner.browser import address, key_event, mouse_event, touch_event


def test_only_web_addresses_are_opened_and_never_the_runners_api():
    assert address("5173") == "http://localhost:5173"
    assert address("5173/orders?x=1") == "http://localhost:5173/orders?x=1"
    assert address("localhost:3000/app?x=1") == "http://localhost:3000/app?x=1"
    assert address("127.0.0.1:8000") == "http://127.0.0.1:8000"
    assert address("example.com/docs") == "https://example.com/docs"
    assert address(" https://example.com ") == "https://example.com"
    for refused in ("", "file:///etc/passwd", "chrome://settings", "javascript:alert(1)", "http://localhost:8090/health", "127.0.0.1:8090", "http://:80"):
        assert address(refused) is None, refused
    assert "*://localhost:8090/*" in browser.blocked_urls()


def test_input_is_translated_for_devtools():
    assert key_event({"type": "down", "key": "a", "code": "KeyA", "keyCode": 65, "mods": 8, "text": "A"}) == {
        "type": "keyDown", "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "modifiers": 8, "text": "A", "unmodifiedText": "A",
    }
    # A key that types nothing is a raw key; releasing never types.
    assert key_event({"type": "down", "key": "ArrowLeft", "code": "ArrowLeft", "keyCode": 37})["type"] == "rawKeyDown"
    assert "text" not in key_event({"type": "up", "key": "a", "code": "KeyA", "keyCode": 65, "text": "a"})
    assert key_event({"type": "press"}) is None

    down = mouse_event({"type": "down", "x": 10.5, "y": 20, "button": 2, "buttons": 2, "clicks": 2, "mods": 2})
    assert down == {"type": "mousePressed", "x": 10.5, "y": 20.0, "modifiers": 2, "buttons": 2, "button": "right", "clickCount": 2}
    assert "button" not in mouse_event({"type": "move", "x": 1, "y": 2})
    assert mouse_event({"type": "drag"}) is None

    assert touch_event({"type": "move", "points": [{"x": 1, "y": 2, "id": 7}]}) == {"type": "touchMove", "touchPoints": [{"x": 1.0, "y": 2.0, "id": 7}]}
    assert touch_event({"type": "end", "points": [{"x": 1, "y": 2, "id": 7}]}) == {"type": "touchEnd", "touchPoints": []}


async def test_a_few_frames_are_on_their_way_at_once_and_no_more():
    """The picture does not wait for one round trip per frame, and still nothing queues up on a slow
    line: beyond a few frames they wait in the browser, not on the way."""
    shown = browser.Browser("pace")
    slow, told = Client(), []

    async def post(method, params):
        told.append(params["sessionId"])

    shown._post = post
    shown.clients.add(slow)
    assert browser.IN_FLIGHT == 3
    await shown._frame_made(b"one", "f1")
    await shown._frame_made(b"two", "f2")
    # The viewer has confirmed nothing yet and gets both; the browser may make the next each time.
    assert slow.frames == [b"one", b"two"] and told == ["f1", "f2"]
    await shown._frame_made(b"three", "f3")
    await shown._frame_made(b"four", None)  # e.g. the sharp picture of the page at rest
    await shown._frame_made(b"five", "f5")
    # Three are on their way: no more are sent, and the browser has not been asked for more.
    assert slow.frames == [b"one", b"two", b"three"] and told == ["f1", "f2"]
    await shown.handle({"t": "ack"}, slow)
    # It gets the newest, not the ones in between, and the browser may go on.
    assert slow.frames == [b"one", b"two", b"three", b"five"] and told == ["f1", "f2", "f5"]
    await shown.handle({"t": "ack"}, slow)
    assert slow.frames[-1] == b"five" and len(slow.frames) == 4  # nothing new: nothing sent
    assert 0 < shown._trip < 1 and shown._rest_after() == browser.REST_SECONDS  # a quick line: the sharp picture follows at once
    shown._trip = 0.3
    assert shown._rest_after() == 1.2  # a slow one: only when the user has probably stopped


class Client:
    """What the browser tells one viewer."""

    def __init__(self):
        self.states: list[dict] = []
        self.frames: list[bytes] = []

    async def send_json(self, message):
        self.states.append(message)

    async def send_bytes(self, frame):
        self.frames.append(frame)

    async def close(self):
        pass


@pytest.mark.skipif(browser.binary() is None, reason="no Chromium in this environment")
async def test_a_page_at_localhost_is_shown_and_typed_into(tmp_path, monkeypatch):
    monkeypatch.setattr(browser, "PROFILES", tmp_path)
    page = b"<!doctype html><title>Shop</title><input id=i autofocus><script>i.oninput=()=>{document.title=i.value;history.replaceState({},'','/typed')}</script>"

    async def serve(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(page), page))
        await writer.drain()
        writer.close()

    site = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = site.sockets[0].getsockname()[1]
    browsers = browser.Browsers()
    client = Client()
    try:
        opened = await browsers.get("test-1")
        await opened.join(client)
        await opened.handle({"t": "size", "w": 640, "h": 480, "dpr": 1, "touch": False})
        await opened.handle({"t": "nav", "url": str(port)})
        await opened.handle({"t": "ack"}, client)
        await wait_for(lambda: client.frames and any(s["url"].endswith(f":{port}/") and not s["loading"] for s in client.states))
        assert client.frames[-1][:2] == b"\xff\xd8"  # a JPEG
        await opened.handle({"t": "text", "text": "hi"})
        # The page changed its own address: here it is the page's, not PlainBoard's.
        await wait_for(lambda: client.states[-1]["url"].endswith("/typed"))
        await opened.handle({"t": "nav", "url": "file:///etc/passwd"})
        assert client.states[-1] == {"t": "error", "message": "This address can't be opened here."}
    finally:
        await browsers.close_all()
        site.close()


async def wait_for(done, seconds: float = 20):
    for _ in range(int(seconds / 0.05)):
        if done():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out")
