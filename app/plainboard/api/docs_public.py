"""Published doc sites without login (#118, services/docs_public.py).

- ``/docs/<slug>/<page path>``: the page as HTML: the frontend's ``docs.html`` (a Vite entry of its own) with the
  page's title, description and canonical address in its head, the page rendered in ``#docs-root`` (what search
  engines read) and ``window.__PB_DOCS__`` = {site, page} for the script; without a frontend build a plain page.
  ``/docs/<slug>/sitemap.xml`` and ``robots.txt`` for search engines.
- ``/api/public/docs/<slug>`` (site and navigation), ``.../page?path=`` (a page), ``.../search?q=``,
  ``.../files/<id>`` (a file of the site): JSON for the script.
- At a site's own verified domain (``DocHost``) the same at the root: ``/<page path>``, ``/sitemap.xml``,
  ``/api/public/docs/<slug>/...`` and the frontend's ``/assets``. Nothing else of PlainBoard answers there.
Unpublished sites and everything else are 404. Each client address has a request budget per minute.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from ..config import get_config
from ..models import DocSite
from ..services import board as board_service
from ..services import docs as docs_service
from ..services import docs_domains
from ..services import docs_public as public
from ..services.errors import NotFound, ServiceError
from .deps import Session
from .guard import _hosts, is_https

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/public/docs", tags=["docs"])  # JSON (services/access.py: /api/public is open)
pages = APIRouter(tags=["docs"])  # the HTML pages: no rule, they check by themselves

PAGES = public.RateLimit(240)
SEARCHES = public.RateLimit(30)
FILES = public.RateLimit(600)
CACHE = {"Cache-Control": "public, max-age=60"}
DOMAIN_SLUG = "docs_domain_slug"  # in the request's state: it came to the site's own domain


def _client(request: Request) -> str:
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",")[0]
    return forwarded.strip() or (request.client.host if request.client else "?")


def _limit(limit: public.RateLimit, request: Request) -> None:
    if not limit.allow(_client(request)):
        raise ServiceError("Too many requests. Try again in a minute.", 429)


def _base(request: Request, site: DocSite) -> str:
    return "" if request.scope.get("state", {}).get(DOMAIN_SLUG) == site.slug else f"/docs/{site.slug}"


async def _site(session, request: Request, slug: str) -> DocSite:  # noqa: ANN001
    on_domain = request.scope.get("state", {}).get(DOMAIN_SLUG)
    if on_domain is not None and on_domain != slug:
        raise NotFound("Not found")  # a site's domain serves that site only
    return await public.published(session, slug)


@router.get("/{slug}")
async def site(slug: str, request: Request, session: Session) -> JSONResponse:
    """{name, slug, description, base, api, project, updated_at, home, nav: [{id, title, path, children}]}"""
    _limit(PAGES, request)
    found = await _site(session, request, slug)
    return JSONResponse(_json(await public.site_view(session, found, _base(request, found))), headers=CACHE)


@router.get("/{slug}/page")
async def page(slug: str, request: Request, session: Session, path: str = Query(default="", max_length=1000)) -> JSONResponse:
    """{id, title, path, html, toc: [{level, id, text}], markdown, description, updated_at, breadcrumbs, prev, next, children}"""
    _limit(PAGES, request)
    found = await _site(session, request, slug)
    return JSONResponse(_json(await public.page_view(session, found, path, _base(request, found))), headers=CACHE)


@router.get("/{slug}/search")
async def search(slug: str, request: Request, session: Session, q: str = Query(min_length=1, max_length=200), limit: int = Query(default=10, ge=1, le=20)) -> JSONResponse:
    """[{title, path, snippet}]: by words, and by meaning where semantic search runs; published pages of this site only."""
    _limit(SEARCHES, request)
    found = await _site(session, request, slug)
    return JSONResponse(_json(await public.search(session, found, q, _base(request, found), limit)))


@router.get("/{slug}/files/{attachment_id}")
async def file(slug: str, attachment_id: int, request: Request, session: Session) -> FileResponse:
    _limit(FILES, request)
    found = await _site(session, request, slug)
    attachment = await docs_service.get_file(session, found, attachment_id)
    path = board_service.storage_path(attachment)
    if not path.exists():
        raise NotFound("Not found")
    shown = board_service.inline_type(path, attachment.filename)  # pictures, PDF, text inline; never a page or an SVG
    headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "public, max-age=3600"}
    if shown != "application/pdf":
        headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    if shown:
        return FileResponse(path, media_type=shown, filename=attachment.filename, content_disposition_type="inline", headers=headers)
    return FileResponse(path, media_type=attachment.content_type, filename=attachment.filename, content_disposition_type="attachment", headers=headers)


def _json(value):  # noqa: ANN001, ANN202
    from fastapi.encoders import jsonable_encoder

    return jsonable_encoder(value)


# --- HTML ------------------------------------------------------------------------------------------------------


def _template() -> str | None:
    path = get_config().static_dir / "docs.html"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _origin(request: Request) -> str:
    hosts = sorted(_hosts(request.headers))
    host = request.headers.get("x-forwarded-host", "").split(",")[0].strip() or request.headers.get("host") or (hosts[0] if hosts else "localhost")
    return f"{'https' if is_https(request) else 'http'}://{host}"


@pages.get("/docs/{slug}", include_in_schema=False)
async def site_root(slug: str, request: Request) -> Response:
    return RedirectResponse(f"/docs/{slug}/", status_code=308)


@pages.get("/docs/{slug}/{path:path}", include_in_schema=False)
async def html_page(slug: str, path: str, request: Request, session: Session) -> Response:
    try:
        _limit(PAGES, request)
        found = await _site(session, request, slug)
    except ServiceError as exc:
        return PlainTextResponse(str(exc), status_code=exc.status_code)
    base = _base(request, found)
    site_url = _origin(request) + base
    if path == "robots.txt":
        return PlainTextResponse(f"User-agent: *\nAllow: /\nSitemap: {site_url}/sitemap.xml\n", headers=CACHE)
    view = _json(await public.site_view(session, found, base))
    if path == "sitemap.xml":
        return Response(public.sitemap(site_url, view), media_type="application/xml", headers=CACHE)
    try:
        page_view = _json(await public.page_view(session, found, path, base)) if view["nav"] else None
    except NotFound:
        return HTMLResponse(public.document(_template(), view, None, site_url + "/"), status_code=404)
    canonical = f"{site_url}/{page_view['path']}" if page_view and path.strip("/") else f"{site_url}/"
    return HTMLResponse(public.document(_template(), view, page_view, canonical), headers=CACHE)


# --- a site's own domain ---------------------------------------------------------------------------------------


class DocHost:
    """A request to a doc site's own domain: that site, its files and the frontend's assets, nothing else."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        found = None
        try:
            for host in _hosts(Headers(scope=scope)):
                if found := await docs_domains.lookup(host.split(":", 1)[0]):
                    break
        except Exception:  # no database yet (starting): PlainBoard as always
            log.debug("Doc domains not readable", exc_info=True)
        if found is None:
            await self.app(scope, receive, send)
            return
        _, slug, token, served = found
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4404})
            return
        path = scope["path"]
        if path == docs_domains.WELL_KNOWN:
            await PlainTextResponse(token)(scope, receive, send)
            return
        if not served:
            await PlainTextResponse("Not found", status_code=404)(scope, receive, send)
            return
        state = {**scope.get("state", {}), DOMAIN_SLUG: slug}
        own_api = f"/api/public/docs/{slug}"
        if path == own_api or path.startswith(own_api + "/") or path.startswith("/assets/"):
            await self.app({**scope, "state": state}, receive, send)
            return
        rewritten = f"/docs/{slug}{path}"
        await self.app({**scope, "path": rewritten, "raw_path": rewritten.encode(), "state": state}, receive, send)
