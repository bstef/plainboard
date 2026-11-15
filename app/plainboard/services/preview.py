"""Preview: web servers that agents start in the runner, opened through PlainBoard at /preview/<port>/.

The previewed server believes it runs at the root of localhost:<port>. What it gets wrong because of
that (absolute paths) is repaired here: redirects are rewritten, and requests that a preview page sends
to the root are sent back under its prefix."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from ..config import get_config
from ..security import SESSION_COOKIE
from .errors import ServiceError

PREFIX = "/preview"
PORT_COOKIE = "plainboard_preview"  # the last previewed port: WebSockets to the root have no other hint
_REFERER_PATH = re.compile(rf"^{PREFIX}/(\d+)(?:/|$)")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "[::1]"}
# Per connection, or decided by this proxy.
_DROP_REQUEST = {"host", "connection", "keep-alive", "upgrade", "te", "trailer", "transfer-encoding", "proxy-authorization", "proxy-connection", "content-length"}
_DROP_RESPONSE = {"connection", "keep-alive", "upgrade", "te", "trailer", "transfer-encoding", "proxy-authenticate", "proxy-connection"}


def runner_host() -> str:
    return urlsplit(get_config().runner_url).hostname or "runner"


def check_port(port: int) -> int:
    """Only ports an agent can open itself, and never the runner's own API."""
    runner_port = urlsplit(get_config().runner_url).port or 80
    if not 1024 <= port <= 65535 or port == runner_port:
        raise ServiceError(f"Port {port} can't be previewed", 404)
    return port


def prefix(port: int) -> str:
    return f"{PREFIX}/{port}"


def target(port: int, path: str, query: str, scheme: str = "http") -> str:
    return f"{scheme}://{runner_host()}:{port}/{path}" + (f"?{query}" if query else "")


def request_headers(headers: list[tuple[str, str]], port: int) -> list[tuple[str, str]]:
    """Headers for the previewed server: it sees itself as localhost (dev servers check that), and
    never the PlainBoard login."""
    out = [("host", f"localhost:{port}")]
    for name, value in headers:
        key = name.lower()
        if key in _DROP_REQUEST:
            continue
        if key == "cookie":
            kept = [c for c in value.split(";") if c.strip().split("=", 1)[0] not in (SESSION_COOKIE, PORT_COOKIE)]
            if not kept:
                continue
            value = ";".join(kept).strip()
        elif key == "origin":
            value = f"http://localhost:{port}"
        elif key == "referer":
            parts = urlsplit(value)
            value = f"http://localhost:{port}" + (parts.path.removeprefix(prefix(port)) or "/") + (f"?{parts.query}" if parts.query else "")
        out.append((name, value))
    return out


def response_headers(headers: list[tuple[str, str]], port: int) -> list[tuple[str, str]]:
    out = []
    for name, value in headers:
        key = name.lower()
        if key in _DROP_RESPONSE:
            continue
        if key == "location":
            value = rewrite_location(value, port)
        out.append((name, value))
    return out


def rewrite_location(location: str, port: int) -> str:
    """A redirect of the previewed server, as a path under its prefix."""
    parts = urlsplit(location)
    own = not parts.netloc or (parts.hostname in _LOCAL_HOSTS | {runner_host()} and parts.port == port)
    if not own or not parts.path.startswith("/") or parts.path.startswith(prefix(port) + "/"):
        return location
    return prefix(port) + parts.path + (f"?{parts.query}" if parts.query else "") + (f"#{parts.fragment}" if parts.fragment else "")


def port_of_referer(referer: str | None, host: str | None) -> int | None:
    """The preview a request comes from, if a preview page on this site sent it."""
    if not referer:
        return None
    parts = urlsplit(referer)
    match = _REFERER_PATH.match(parts.path)
    if not match or parts.netloc != host:
        return None
    return int(match[1])


def port_of_cookie(value: str | None) -> int | None:
    return int(value) if value and value.isdigit() else None


def unreachable(port: int) -> str:
    return (
        f"Nothing answers on port {port} in the agents' environment.\n\n"
        "The server may not be running (anymore), or it only listens on 127.0.0.1: it has to listen on 0.0.0.0."
    )
