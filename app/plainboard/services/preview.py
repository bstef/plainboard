"""Preview: web servers that agents start in the runner, opened through PlainBoard at /preview/<port>/.

The previewed server believes it runs at the root of localhost:<port>. What it gets wrong because of
that (absolute paths) is repaired here: redirects are rewritten, and requests that a preview page sends
to the root are sent back under its prefix."""

from __future__ import annotations

import hashlib
import hmac
import html
import re
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_config
from ..models import PreviewShare, utcnow
from ..security import SESSION_COOKIE, sign, unsign
from .errors import ServiceError

PREFIX = "/preview"
PORT_COOKIE = "plainboard_preview"  # the last previewed port: WebSockets to the root have no other hint
UNLOCK_COOKIE_PREFIX = "pb_preview_key_"  # per port: the visitor gave the password of a shared preview
UNLOCK_MAX_AGE = 60 * 60 * 24 * 7
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
            kept = [c for c in value.split(";") if c.strip().split("=", 1)[0] not in (SESSION_COOKIE, PORT_COOKIE) and not c.strip().startswith(UNLOCK_COOKIE_PREFIX)]
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


# --- sharing: without a share only the signed-in user opens a preview ---------------------------


def _hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    return f"{salt}${hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 200_000).hex()}"


async def share(
    session: AsyncSession, project_id: int, port: int, access: str, *, password: str | None = None, days: int = 7, agent_id: int | None = None
) -> tuple[PreviewShare | None, str | None]:
    """Set who may open a preview without the login. Returns the share (None: private again) and the
    password if one was generated."""
    check_port(port)
    existing = await session.scalar(select(PreviewShare).where(PreviewShare.port == port))
    if existing and existing.project_id != project_id and existing.expires_at > utcnow():
        raise ServiceError(f"Port {port} is shared by another project")
    if existing:
        await session.delete(existing)
        await session.flush()
    if access == "private":
        await session.commit()
        return None, None
    generated = None
    if access == "password" and not password:
        password = generated = secrets.token_urlsafe(9)
    row = PreviewShare(
        project_id=project_id,
        port=port,
        access=access,
        password_hash=_hash(password) if access == "password" else None,
        created_by_agent_id=agent_id,
        expires_at=utcnow() + timedelta(days=days),
    )
    session.add(row)
    await session.commit()
    return row, generated


async def active_share(session: AsyncSession, port: int) -> PreviewShare | None:
    return await session.scalar(select(PreviewShare).where(PreviewShare.port == port, PreviewShare.expires_at > utcnow()))


async def shares_for_project(session: AsyncSession, project_id: int) -> list[PreviewShare]:
    await session.execute(delete(PreviewShare).where(PreviewShare.expires_at <= utcnow()))
    await session.commit()
    return list(await session.scalars(select(PreviewShare).where(PreviewShare.project_id == project_id).order_by(PreviewShare.port)))


def _salt(row: PreviewShare) -> str:
    return (row.password_hash or "$").split("$", 1)[0]


def password_ok(row: PreviewShare, given: str) -> bool:
    return bool(row.password_hash) and hmac.compare_digest(_hash(given, _salt(row)), row.password_hash)


def unlock_cookie(port: int) -> str:
    return f"{UNLOCK_COOKIE_PREFIX}{port}"


def unlock_value(row: PreviewShare) -> str:
    return sign({"share": row.id, "salt": _salt(row)}, "preview")


def unlocked(row: PreviewShare, cookie: str | None) -> bool:
    """A new share or password on the same port needs the password again (its salt is new)."""
    data = unsign(cookie, "preview", UNLOCK_MAX_AGE)
    return bool(data) and data.get("share") == row.id and data.get("salt") == _salt(row)


def unlock_page(port: int, wrong: bool = False) -> str:
    error = '<p class="e">Wrong password.</p>' if wrong else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Protected preview</title>
<style>body{{font:16px system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;background:#f4f4f5;color:#18181b}}
form{{background:#fff;padding:28px;border-radius:14px;box-shadow:0 1px 3px #0002;width:min(88vw,320px)}}h1{{font-size:18px;margin:0 0 6px}}
p{{margin:0 0 16px;color:#52525b;font-size:14px}}.e{{color:#b91c1c}}input,button{{font:inherit;width:100%;box-sizing:border-box;padding:10px 12px;border-radius:8px}}
input{{border:1px solid #d4d4d8;margin-bottom:12px}}button{{border:0;background:#4f46e5;color:#fff;cursor:pointer}}
@media(prefers-color-scheme:dark){{body{{background:#18181b;color:#fafafa}}form{{background:#27272a}}p{{color:#a1a1aa}}input{{background:#18181b;color:#fafafa;border-color:#3f3f46}}}}</style></head>
<body><form method="post" action="/api/public/preview/{html.escape(str(port))}/unlock"><h1>Protected preview</h1><p>Enter the password you were given.</p>{error}
<input type="password" name="password" autofocus required autocomplete="current-password" aria-label="Password"><button>Open</button></form></body></html>"""
