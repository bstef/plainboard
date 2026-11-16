"""A browser next to the agents, shown in PlainBoard.

A server an agent starts believes it runs at the root of localhost:<port>. Opened through PlainBoard's
address that is only nearly true, and some pages break on it (see the app's services/preview.py).
This browser runs here, where localhost:<port> is what the page thinks it is, so every page works.
PlainBoard shows its picture and sends back what the user does.

One Chromium per key (a project), driven over the DevTools protocol. A client is a WebSocket:

    to the client   bytes                                  a frame of the page (JPEG)
                    {"t": "state", url, title, loading, back, forward}
                    {"t": "dialog", kind, message}          alert, confirm, prompt, beforeunload
                    {"t": "error", message}
    from the client {"t": "size", w, h, dpr, touch}         the size of the picture, in CSS pixels
                    {"t": "nav", url} | {"t": "back"} | {"t": "forward"} | {"t": "reload"}
                    {"t": "mouse", type: down|up|move, x, y, button, buttons, clicks, mods}
                    {"t": "wheel", x, y, dx, dy, mods}
                    {"t": "touch", type: start|move|end|cancel, points: [{x, y, id}]}
                    {"t": "key", type: down|up, key, code, keyCode, mods, text}
                    {"t": "text", text}                     typed or pasted text
                    {"t": "dialog", accept, text}

``mods`` is the DevTools bit mask: Alt 1, Ctrl 2, Meta 4, Shift 8.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

log = logging.getLogger(__name__)

PROFILES = Path.home() / ".plainboard" / "browser"  # in the permanent home: sign-ins survive a restart
BINARIES = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")
RUNNER_PORT = 8090
MAX_BROWSERS = 3
IDLE_SECONDS = 600  # a browser nobody looks at is closed after this long
START_SECONDS = 20
KEY = re.compile(r"[a-z0-9-]{1,40}")
MIN_SIDE, MAX_SIDE = 200, 4000
_MOUSE = {"down": "mousePressed", "up": "mouseReleased", "move": "mouseMoved"}
_BUTTON = {0: "left", 1: "middle", 2: "right"}
_TOUCH = {"start": "touchStart", "move": "touchMove", "end": "touchEnd", "cancel": "touchCancel"}
INPUT = {"mouse", "wheel", "touch", "key", "text"}  # in the order it was made; everything else may overtake


class BrowserError(Exception):
    pass


def binary() -> str | None:
    return next((path for name in BINARIES if (path := shutil.which(name))), None)


def address(text: str) -> str | None:
    """What the user typed, as an address the browser may open: a port, a host, or a full address.
    Only http(s): no files of the runner, no pages of the browser itself, and not the runner's API."""
    text = text.strip()
    if not text:
        return None
    if port_only := re.fullmatch(r"(\d{2,5})(/.*)?", text):  # "5173", "5173/orders"
        text = f"localhost:{port_only[1]}{port_only[2] or ''}"
    if "://" not in text:
        host = text.split("/", 1)[0].split(":", 1)[0]
        local = host == "localhost" or host.endswith(".localhost") or re.fullmatch(r"[\d.]+", host) is not None
        text = ("http://" if local else "https://") + text
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port == RUNNER_PORT and parts.hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "runner"):
        return None
    return text


def blocked_urls() -> list[str]:
    """The runner's own API runs commands: no page may ask it anything."""
    return [f"*://{host}:{RUNNER_PORT}/*" for host in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "runner")]


def mouse_event(command: dict[str, Any]) -> dict[str, Any] | None:
    kind = _MOUSE.get(command.get("type", ""))
    if kind is None:
        return None
    event = {"type": kind, "x": _number(command.get("x")), "y": _number(command.get("y")), "modifiers": int(command.get("mods") or 0), "buttons": int(command.get("buttons") or 0)}
    if kind != "mouseMoved":
        event |= {"button": _BUTTON.get(command.get("button"), "left"), "clickCount": max(1, int(command.get("clicks") or 1))}
    return event


def key_event(command: dict[str, Any]) -> dict[str, Any] | None:
    """A key as DevTools wants it: a key that types something is a keyDown with its text, any other a rawKeyDown."""
    if command.get("type") not in ("down", "up"):
        return None
    text = str(command.get("text") or "")
    event = {
        "type": "keyUp" if command["type"] == "up" else "keyDown" if text else "rawKeyDown",
        "key": str(command.get("key") or ""),
        "code": str(command.get("code") or ""),
        "windowsVirtualKeyCode": int(command.get("keyCode") or 0),
        "modifiers": int(command.get("mods") or 0),
    }
    if text and command["type"] == "down":
        event |= {"text": text, "unmodifiedText": text}
    return event


def touch_event(command: dict[str, Any]) -> dict[str, Any] | None:
    kind = _TOUCH.get(command.get("type", ""))
    if kind is None:
        return None
    points = [{"x": _number(p.get("x")), "y": _number(p.get("y")), "id": int(p.get("id") or 0)} for p in command.get("points") or [] if isinstance(p, dict)]
    return {"type": kind, "touchPoints": [] if kind in ("touchEnd", "touchCancel") else points}


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class Cdp:
    """One connection to the browser's DevTools: commands with their answers, and events in order."""

    def __init__(self, socket: ClientConnection):
        self._socket = socket
        self._next = 0
        self._waiting: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.events: asyncio.Queue[tuple[str, dict[str, Any], str | None] | None] = asyncio.Queue()
        self._reader = asyncio.create_task(self._read(), name="browser-cdp")

    async def _read(self) -> None:
        try:
            async for raw in self._socket:
                message = json.loads(raw)
                if "id" in message:
                    future = self._waiting.pop(message["id"], None)
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(BrowserError(message["error"].get("message", "DevTools error")))
                        else:
                            future.set_result(message.get("result", {}))
                elif "method" in message:
                    self.events.put_nowait((message["method"], message.get("params", {}), message.get("sessionId")))
        except ConnectionClosed:
            pass
        finally:
            for future in self._waiting.values():
                if not future.done():
                    future.set_exception(BrowserError("The browser closed"))
            self._waiting.clear()
            self.events.put_nowait(None)

    async def send(self, method: str, params: dict[str, Any] | None = None, session: str | None = None) -> dict[str, Any]:
        self._next += 1
        message: dict[str, Any] = {"id": self._next, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiting[self._next] = future
        try:
            await self._socket.send(json.dumps(message))
        except ConnectionClosed as exc:
            self._waiting.pop(message["id"], None)
            raise BrowserError("The browser closed") from exc
        try:
            return await asyncio.wait_for(future, timeout=30)
        except TimeoutError as exc:
            self._waiting.pop(message["id"], None)
            raise BrowserError("The browser did not answer") from exc

    async def post(self, method: str, params: dict[str, Any], session: str | None = None) -> None:
        """Send without waiting for the answer: for input, which a page answers only once it is free
        again (a page that shows a dialog is not)."""
        self._next += 1
        message: dict[str, Any] = {"id": self._next, "method": method, "params": params}
        if session:
            message["sessionId"] = session
        try:
            await self._socket.send(json.dumps(message))
        except ConnectionClosed as exc:
            raise BrowserError("The browser closed") from exc

    async def close(self) -> None:
        self._reader.cancel()
        with contextlib.suppress(Exception):
            await self._socket.close()


class Browser:
    """One Chromium with one page, and the clients that look at it."""

    def __init__(self, key: str):
        self.key = key
        self.clients: set[WebSocket] = set()
        self.left_at = time.monotonic()
        self.closed = False
        self._process: asyncio.subprocess.Process | None = None
        self._cdp: Cdp | None = None
        self._target = ""
        self._page: str | None = None
        self._worker: asyncio.Task[None] | None = None
        self._frame: bytes | None = None
        self._state: dict[str, Any] = {"t": "state", "url": "", "title": "", "loading": False, "back": False, "forward": False}
        self._size = (1280, 800, 1.0, False)
        self._casting = False

    # --- life --------------------------------------------------------------------------------

    async def start(self) -> None:
        executable = binary()
        if executable is None:
            raise BrowserError("No browser is installed in the runner. Rebuild it: docker compose up -d --build")
        profile = PROFILES / self.key
        profile.mkdir(parents=True, exist_ok=True)
        for stale in (*profile.glob("Singleton*"), profile / "DevToolsActivePort"):  # left by a browser that was killed
            with contextlib.suppress(OSError):
                stale.unlink()
        self._process = await asyncio.create_subprocess_exec(
            executable,
            "--headless=new",
            "--no-sandbox",  # the container is the sandbox: agents have root in it anyway
            "--disable-gpu",
            "--disable-dev-shm-usage",  # Docker's /dev/shm is small
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
            f"--window-size={self._size[0]},{self._size[1]}",
            "about:blank",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        endpoint = await self._endpoint(profile / "DevToolsActivePort")
        self._cdp = Cdp(await connect(endpoint, max_size=None, open_timeout=10, ping_interval=None))
        targets = (await self._cdp.send("Target.getTargets"))["targetInfos"]
        page = next((t for t in targets if t["type"] == "page"), None)
        self._target = page["targetId"] if page else (await self._cdp.send("Target.createTarget", {"url": "about:blank"}))["targetId"]
        self._page = (await self._cdp.send("Target.attachToTarget", {"targetId": self._target, "flatten": True}))["sessionId"]
        await self._cdp.send("Target.setDiscoverTargets", {"discover": True})
        with contextlib.suppress(BrowserError):
            await self._cdp.send("Browser.setDownloadBehavior", {"behavior": "deny"})
        await self._send("Page.enable")
        await self._send("Network.enable")
        await self._send("Network.setBlockedURLs", {"urls": blocked_urls()})
        await self._apply_size()
        self._worker = asyncio.create_task(self._work(), name=f"browser-{self.key}")

    async def _endpoint(self, port_file: Path) -> str:
        """Chromium writes the port it chose and the path of its DevTools socket into its profile."""
        deadline = time.monotonic() + START_SECONDS
        while time.monotonic() < deadline:
            if self._process and self._process.returncode is not None:
                raise BrowserError("The browser did not start")
            with contextlib.suppress(OSError):
                lines = port_file.read_text().split()
                if len(lines) >= 2:
                    return f"ws://127.0.0.1:{lines[0]}{lines[1]}"
            await asyncio.sleep(0.1)
        raise BrowserError("The browser did not start in time")

    async def stop(self) -> None:
        self.closed = True
        if self._worker and self._worker is not asyncio.current_task():
            self._worker.cancel()
        if self._cdp:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._cdp.send("Browser.close"), timeout=3)
            await self._cdp.close()
        if self._process and self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self._process.kill()
        for client in list(self.clients):
            with contextlib.suppress(Exception):
                await client.close()
        self.clients.clear()

    # --- clients -----------------------------------------------------------------------------

    async def join(self, client: WebSocket) -> None:
        self.clients.add(client)
        await client.send_json(self._state)
        if self._frame:
            await client.send_bytes(self._frame)
        await self._cast(True)

    async def leave(self, client: WebSocket) -> None:
        self.clients.discard(client)
        if not self.clients:
            self.left_at = time.monotonic()
            with contextlib.suppress(BrowserError):
                await self._cast(False)  # no frames for nobody

    async def _tell(self, message: dict[str, Any] | bytes) -> None:
        for client in list(self.clients):
            try:
                await (client.send_bytes(message) if isinstance(message, bytes) else client.send_json(message))
            except Exception:  # it is gone; its own loop notices
                self.clients.discard(client)

    # --- what the client asks ----------------------------------------------------------------

    async def handle(self, command: dict[str, Any]) -> None:
        """Input (see INPUT) is passed on at once and never waited for; the rest returns when it is done."""
        kind = command.get("t")
        if kind == "size":
            width = int(min(MAX_SIDE, max(MIN_SIDE, _number(command.get("w")))))
            height = int(min(MAX_SIDE, max(MIN_SIDE, _number(command.get("h")))))
            size = (width, height, min(2.0, max(1.0, _number(command.get("dpr")) or 1.0)), bool(command.get("touch")))
            if size != self._size:
                self._size = size
                await self._apply_size()
        elif kind == "nav":
            url = address(str(command.get("url") or ""))
            if url is None:
                await self._tell({"t": "error", "message": "This address can't be opened here."})
                return
            answer = await self._send("Page.navigate", {"url": url})
            if answer.get("errorText"):
                await self._tell({"t": "error", "message": f"{url}: {answer['errorText']}"})
        elif kind in ("back", "forward"):
            history = await self._send("Page.getNavigationHistory")
            index = history["currentIndex"] + (-1 if kind == "back" else 1)
            if 0 <= index < len(history["entries"]):
                await self._send("Page.navigateToHistoryEntry", {"entryId": history["entries"][index]["id"]})
        elif kind == "reload":
            await self._send("Page.reload")
        elif kind == "mouse":
            if event := mouse_event(command):
                await self._post("Input.dispatchMouseEvent", event)
        elif kind == "wheel":
            await self._post(
                "Input.dispatchMouseEvent",
                {"type": "mouseWheel", "x": _number(command.get("x")), "y": _number(command.get("y")), "deltaX": _number(command.get("dx")), "deltaY": _number(command.get("dy")), "modifiers": int(command.get("mods") or 0)},
            )
        elif kind == "touch":
            if event := touch_event(command):
                await self._post("Input.dispatchTouchEvent", event)
        elif kind == "key":
            if event := key_event(command):
                await self._post("Input.dispatchKeyEvent", event)
        elif kind == "text":
            if text := str(command.get("text") or ""):
                await self._post("Input.insertText", {"text": text})
        elif kind == "dialog":
            with contextlib.suppress(BrowserError):  # already answered by another client
                await self._send("Page.handleJavaScriptDialog", {"accept": bool(command.get("accept")), "promptText": str(command.get("text") or "")})

    # --- the browser -------------------------------------------------------------------------

    async def _send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._cdp is None or self.closed:
            raise BrowserError("The browser is closed")
        return await self._cdp.send(method, params, self._page)

    async def _post(self, method: str, params: dict[str, Any]) -> None:
        if self._cdp is None or self.closed:
            raise BrowserError("The browser is closed")
        await self._cdp.post(method, params, self._page)

    async def _apply_size(self) -> None:
        width, height, scale, touch = self._size
        await self._send("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": scale, "mobile": touch})
        await self._send("Emulation.setTouchEmulationEnabled", {"enabled": touch})
        if self._casting:  # frames of the new size
            self._casting = False
            await self._send("Page.stopScreencast")
            await self._cast(True)

    async def _cast(self, on: bool) -> None:
        if on == self._casting:
            return
        self._casting = on
        if on:
            width, height, scale, _ = self._size
            await self._send("Page.startScreencast", {"format": "jpeg", "quality": 75, "maxWidth": int(width * scale), "maxHeight": int(height * scale), "everyNthFrame": 1})
        else:
            await self._send("Page.stopScreencast")

    async def _work(self) -> None:
        """Takes the browser's events one after the other."""
        assert self._cdp is not None
        while True:
            event = await self._cdp.events.get()
            if event is None:  # the browser is gone
                if not self.closed:
                    await self._tell({"t": "error", "message": "The browser closed. Open the address again."})
                    await browsers.drop(self)
                return
            try:
                await self._on(*event)
            except BrowserError as exc:
                log.info("Browser %s: %s", self.key, exc)
            except Exception:
                log.exception("Browser %s: event failed", self.key)

    async def _on(self, method: str, params: dict[str, Any], session: str | None) -> None:
        if method == "Page.screencastFrame":
            self._frame = base64.b64decode(params["data"])
            await self._send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
            await self._tell(self._frame)
        elif method in ("Page.frameNavigated", "Page.navigatedWithinDocument"):
            frame = params.get("frame", {})
            if method == "Page.navigatedWithinDocument" or not frame.get("parentId"):
                await self._changed(url=params.get("url") or frame.get("url", ""))
        elif method == "Page.frameStartedLoading" and params.get("frameId") == self._target:
            await self._changed(loading=True)
        elif method == "Page.frameStoppedLoading" and params.get("frameId") == self._target:
            await self._changed(loading=False)
        elif method == "Page.javascriptDialogOpening":
            await self._tell({"t": "dialog", "kind": params.get("type", "alert"), "message": params.get("message", ""), "text": params.get("defaultPrompt", "")})
        elif method == "Target.targetInfoChanged" and params["targetInfo"]["targetId"] == self._target:
            await self._changed(title=params["targetInfo"].get("title", ""))
        elif method == "Target.targetCreated":
            info = params["targetInfo"]
            if info["type"] == "page" and info["targetId"] != self._target:
                await self._popup(info["targetId"])
        elif method == "Inspector.targetCrashed":
            await self._tell({"t": "error", "message": "The page crashed. Reload it."})

    async def _changed(self, **values: Any) -> None:
        state = self._state | values
        if "url" in values or values.get("loading") is False:  # the history also knows the title the page ended up with
            with contextlib.suppress(BrowserError):
                history = await self._send("Page.getNavigationHistory")
                index = history["currentIndex"]
                state |= {"back": index > 0, "forward": index < len(history["entries"]) - 1}
                if "title" not in values and 0 <= index < len(history["entries"]):
                    state["title"] = history["entries"][index].get("title") or state["title"]
        if state != self._state:
            self._state = state
            await self._tell(state)

    async def _popup(self, target: str) -> None:
        """There is one page: what a page opens in a new window is opened in this one."""
        assert self._cdp is not None
        url = ""
        for _ in range(20):  # the address arrives a moment after the window
            info = (await self._cdp.send("Target.getTargetInfo", {"targetId": target}))["targetInfo"]
            url = info.get("url", "")
            if url and url != "about:blank":
                break
            await asyncio.sleep(0.05)
        with contextlib.suppress(BrowserError):
            await self._cdp.send("Target.closeTarget", {"targetId": target})
        if address(url):
            await self._send("Page.navigate", {"url": url})


class Browsers:
    """The open browsers, by key. Few at a time: each one is a Chromium."""

    def __init__(self) -> None:
        self._open: dict[str, Browser] = {}
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task[None] | None = None

    async def get(self, key: str) -> Browser:
        async with self._lock:
            browser = self._open.get(key)
            if browser and not browser.closed:
                return browser
            unwatched = sorted((b for b in self._open.values() if not b.clients), key=lambda b: b.left_at)
            if len(self._open) >= MAX_BROWSERS:
                if not unwatched:
                    raise BrowserError(f"{MAX_BROWSERS} browsers are open already. Close one first.")
                await self._close(unwatched[0])
            browser = Browser(key)
            try:
                await browser.start()
            except BaseException:
                await browser.stop()
                raise
            self._open[key] = browser
            if self._reaper is None or self._reaper.done():
                self._reaper = asyncio.create_task(self._reap(), name="browser-reaper")
            return browser

    async def drop(self, browser: Browser) -> None:
        async with self._lock:
            await self._close(browser)

    async def _close(self, browser: Browser) -> None:
        if self._open.get(browser.key) is browser:
            del self._open[browser.key]
        await browser.stop()

    async def _reap(self) -> None:
        while self._open:
            await asyncio.sleep(30)
            async with self._lock:
                for browser in list(self._open.values()):
                    if not browser.clients and time.monotonic() - browser.left_at > IDLE_SECONDS:
                        await self._close(browser)

    async def close_all(self) -> None:
        async with self._lock:
            for browser in list(self._open.values()):
                await self._close(browser)

    async def serve(self, client: WebSocket, key: str) -> None:
        """One client until it leaves."""
        await client.accept()
        if not KEY.fullmatch(key):
            await client.close(code=4404)
            return
        try:
            browser = await self.get(key)
        except (BrowserError, OSError, TimeoutError) as exc:
            await client.send_json({"t": "error", "message": str(exc) or "The browser did not start"})
            await client.close()
            return
        running: set[asyncio.Task[None]] = set()

        async def run(command: dict[str, Any]) -> None:
            try:
                await browser.handle(command)
            except BrowserError as exc:
                with contextlib.suppress(Exception):
                    await client.send_json({"t": "error", "message": str(exc)})
            except (TypeError, ValueError, KeyError):
                pass  # a malformed command

        try:
            await browser.join(client)
            while True:
                command = await client.receive_json()
                if not isinstance(command, dict):
                    continue
                if command.get("t") in INPUT:
                    await run(command)
                else:
                    # Not in line behind the others: opening an address can wait for a dialog of the
                    # page that is left, and the answer to that dialog is the next command.
                    task = asyncio.create_task(run(command))
                    running.add(task)
                    task.add_done_callback(running.discard)
        except (WebSocketDisconnect, RuntimeError, json.JSONDecodeError):
            pass
        finally:
            for task in running:
                task.cancel()
            await browser.leave(client)


browsers = Browsers()
