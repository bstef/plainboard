"""What a published doc site shows the world (#118): pages rendered on the server, the navigation, the search.

Markdown is rendered with markdown-it (CommonMark, tables, strikethrough), raw HTML off: whatever an agent writes,
no script reaches the site. ``doc-file:<id>`` and ``doc-page:<id>`` become the file's and the page's address, every
heading gets an id for the table of contents. Only pages of a published site, only its own files: nothing else of
the project is reachable without login.
"""

from __future__ import annotations

import html
import re
import time
from collections import deque
from dataclasses import dataclass

from markdown_it import MarkdownIt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DocPage, DocSite, Project
from . import docs as docs_service
from .errors import NotFound
from .util import slugify

_md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False}).enable(["table", "strikethrough"])
_WORDS = re.compile(r"[^\W_]+")


@dataclass
class Rendered:
    html: str
    toc: list[dict]  # {level, id, text}
    text: str  # plain text: the description, the search


def _url(base: str, path: str) -> str:
    return f"{base}/{path}" if path else f"{base}/"


def render(body: str, *, base: str, files_base: str, page_paths: dict[int, str]) -> Rendered:
    """``base``: the site's address ("" on its own domain, /docs/<slug> here); ``files_base``: where its files are."""
    tokens = _md.parse(body or "")
    toc: list[dict] = []
    used: set[str] = set()
    words: list[str] = []
    for i, token in enumerate(tokens):
        if token.type == "heading_open" and i + 1 < len(tokens):
            text = tokens[i + 1].content
            anchor, n = slugify(text, "section"), 2
            while anchor in used:
                anchor, n = f"{slugify(text, 'section')}-{n}", n + 1
            used.add(anchor)
            token.attrSet("id", anchor)
            toc.append({"level": int(token.tag[1]), "id": anchor, "text": text})
        for child in token.children or []:
            for attr in ("href", "src"):
                value = child.attrGet(attr)
                if isinstance(value, str):
                    child.attrSet(attr, _resolve(value, base, files_base, page_paths))
            if child.type == "text":
                words.append(child.content)
        if token.type == "inline":
            words.append("\n")
    return Rendered(_md.renderer.render(tokens, _md.options, {}), toc, re.sub(r"\s+\n", "\n", " ".join(words)).strip())


def _resolve(value: str, base: str, files_base: str, page_paths: dict[int, str]) -> str:
    if m := re.fullmatch(r"doc-file:(\d+)", value):
        return f"{files_base}/{m.group(1)}"
    if m := re.fullmatch(r"doc-page:(\d+)(#[\w-]+)?", value):
        path = page_paths.get(int(m.group(1)))
        return _url(base, path) + (m.group(2) or "") if path is not None else "#"
    return value


def describe(text: str, size: int = 160) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= size else flat[: size - 1].rsplit(" ", 1)[0] + "…"


# --- the site -------------------------------------------------------------------------------------------------


async def published(session: AsyncSession, slug: str) -> DocSite:
    site = await session.scalar(select(DocSite).where(DocSite.slug == slug))
    if site is None or not site.published:
        raise NotFound("Not found")
    return site


def api_base(site: DocSite) -> str:
    return f"/api/public/docs/{site.slug}"


def nav(rows: list[DocPage], paths: dict[int, str]) -> list[dict]:
    """The tree: {id, title, path, children}."""
    nodes = {p.id: {"id": p.id, "title": p.title, "path": paths[p.id], "children": []} for p in rows}
    top: list[dict] = []
    for p in rows:  # tree order: a parent comes before its children
        (nodes[p.parent_id]["children"] if p.parent_id in nodes else top).append(nodes[p.id])
    return top


async def site_view(session: AsyncSession, site: DocSite, base: str) -> dict:
    rows = await docs_service.pages(session, site.id)
    paths = docs_service.url_paths(rows)
    project = await session.get(Project, site.project_id)
    return {
        "name": site.name, "slug": site.slug, "description": site.description, "base": base, "api": api_base(site),
        "project": project.name if project else "", "updated_at": max([site.updated_at, *(p.edited_at for p in rows)]),
        "home": paths[rows[0].id] if rows else None, "nav": nav(rows, paths),
    }  # fmt: skip


async def page_view(session: AsyncSession, site: DocSite, path: str, base: str) -> dict:
    """A page by its address under the site ("" : the home page, the first one at the top)."""
    rows = await docs_service.pages(session, site.id)
    if not rows:
        raise NotFound("Not found")
    paths = docs_service.url_paths(rows)
    path = path.strip("/")
    page = rows[0] if not path else next((p for p in rows if paths[p.id] == path), None)
    if page is None:
        raise NotFound("Not found")
    by_id = {p.id: p for p in rows}
    rendered = render(page.body, base=base, files_base=f"{api_base(site)}/files", page_paths=paths)
    at = rows.index(page)
    crumbs, up = [], by_id.get(page.parent_id) if page.parent_id else None
    while up is not None and len(crumbs) < 20:
        crumbs.insert(0, {"title": up.title, "path": paths[up.id]})
        up = by_id.get(up.parent_id) if up.parent_id else None
    near = lambda p: {"title": p.title, "path": paths[p.id]} if p else None  # noqa: E731
    return {
        "id": page.id, "title": page.title, "path": paths[page.id], "html": rendered.html, "toc": rendered.toc, "markdown": page.body,
        "description": describe(rendered.text) or site.description, "updated_at": page.edited_at, "breadcrumbs": crumbs,
        "prev": near(rows[at - 1] if at > 0 else None), "next": near(rows[at + 1] if at + 1 < len(rows) else None),
        "children": [near(p) for p in rows if p.parent_id == page.id],
    }  # fmt: skip


# --- search ---------------------------------------------------------------------------------------------------


def _terms(query: str) -> list[str]:
    from .search.store import STOPWORDS

    return [w for w in dict.fromkeys(_WORDS.findall(query.lower())) if len(w) >= 2 and w not in STOPWORDS][:12]


def _snippet(text: str, terms: list[str], size: int = 200) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    lowered = flat.lower()
    at = min((i for t in terms if (i := lowered.find(t)) >= 0), default=0)
    start = max(0, at - size // 3)
    return ("…" if start else "") + flat[start : start + size].strip() + ("…" if start + size < len(flat) else "")


async def search(session: AsyncSession, site: DocSite, query: str, base: str, limit: int = 10) -> list[dict]:
    """By words (every page, title words count three times) and, where semantic search runs, by meaning over the
    site's pages; fused by rank. Each hit: title, path, snippet."""
    rows = await docs_service.pages(session, site.id)
    if not rows or not query.strip():
        return []
    paths = docs_service.url_paths(rows)
    terms = _terms(query)
    texts = {p.id: render(p.body, base=base, files_base="", page_paths=paths).text for p in rows}
    scored = []
    for p in rows:
        title, body = p.title.lower(), texts[p.id].lower()
        score = sum(3 * title.count(t) + min(body.count(t), 10) for t in terms)
        if score:
            scored.append((score, p.id))
    by_words = [pid for _, pid in sorted(scored, key=lambda x: -x[0])]
    by_meaning = await _by_meaning(session, site, query, [p.id for p in rows])
    fused: dict[int, float] = {}
    for ranking in (by_words, by_meaning):
        for rank, pid in enumerate(ranking):
            fused[pid] = fused.get(pid, 0.0) + 1.0 / (60 + rank)
    by_id = {p.id: p for p in rows}
    best = sorted(fused, key=lambda pid: -fused[pid])[:limit]
    return [{"title": by_id[pid].title, "path": paths[pid], "snippet": _snippet(texts[pid], terms or [query.lower()])} for pid in best]


async def _by_meaning(session: AsyncSession, site: DocSite, query: str, page_ids: list[int]) -> list[int]:
    from . import search as search_service
    from .search import store

    project = await session.get(Project, site.project_id)
    if project is None or not await search_service.enabled(session, project):
        return []
    model = await search_service.embedder(session)
    if model.state != "ready":
        return []
    hits = await store.search(session, project.id, query, await model.query(query), model.name, kinds=["doc"], source_ids=[str(i) for i in page_ids], limit=10)
    return [int(h.source_id) for h in hits if (h.similarity or 0) > 0 and h.vector_rank is not None]


# --- for search engines ---------------------------------------------------------------------------------------


def sitemap(site_url: str, view: dict) -> str:
    urls: list[str] = []

    def walk(nodes: list[dict]) -> None:
        for node in nodes:
            urls.append(f"{site_url}/{node['path']}")
            walk(node["children"])

    walk(view["nav"])
    items = "".join(f"<url><loc>{html.escape(u)}</loc></url>" for u in [f"{site_url}/", *urls])
    return f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</urlset>'


def _nav_html(nodes: list[dict], base: str, current: str) -> str:
    if not nodes:
        return ""
    items = []
    for n in nodes:
        mark = ' aria-current="page"' if n["path"] == current else ""
        items.append(f'<li><a href="{html.escape(_url(base, n["path"]))}"{mark}>{html.escape(n["title"])}</a>{_nav_html(n["children"], base, current)}</li>')
    return f"<ul>{''.join(items)}</ul>"


def head_html(site_view_: dict, page: dict | None, canonical: str) -> str:
    title = f"{page['title']} – {site_view_['name']}" if page else site_view_["name"]
    description = (page or {}).get("description") or site_view_["description"]
    e = html.escape
    return (
        f"<title>{e(title)}</title>"
        f'<meta name="description" content="{e(description)}">'
        f'<link rel="canonical" href="{e(canonical)}">'
        f'<meta property="og:type" content="article"><meta property="og:title" content="{e(title)}">'
        f'<meta property="og:description" content="{e(description)}"><meta property="og:url" content="{e(canonical)}">'
        f'<meta property="og:site_name" content="{e(site_view_["name"])}">'
    )


def body_html(site_view_: dict, page: dict | None) -> str:
    """The page as plain HTML: what a search engine reads, and what shows before (or without) the site's script."""
    e, base = html.escape, site_view_["base"]
    article = (
        f'<article><h1>{e(page["title"])}</h1>{page["html"]}</article>'
        if page
        else f"<article><h1>{e(site_view_['name'])}</h1><p>{e(site_view_['description'])}</p></article>"
    )
    return f'<nav aria-label="{e(site_view_["name"])}"><a href="{e(_url(base, ""))}">{e(site_view_["name"])}</a>{_nav_html(site_view_["nav"], base, (page or {}).get("path", ""))}</nav><main>{article}</main>'


FALLBACK = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<!--pb-docs-head--><style>body{font:16px/1.6 system-ui,sans-serif;margin:0;display:flex;gap:2rem;color:#1f2328}nav{min-width:15rem;padding:1.5rem;border-right:1px solid #ddd}
nav ul{list-style:none;padding-left:1rem}main{max-width:48rem;padding:1.5rem}pre{background:#f6f8fa;padding:1rem;overflow:auto}img{max-width:100%}
table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:.3rem .6rem}a[aria-current]{font-weight:600}</style></head>
<body><div id="docs-root"><!--pb-docs-body--></div></body></html>"""


def document(template: str | None, site_view_: dict, page: dict | None, canonical: str) -> str:
    """The HTML of a page: the frontend's ``docs.html`` (or a plain one without a build) with the page's title,
    description and canonical address in its head, the rendered page in its body and the state for the script
    (``window.__PB_DOCS__``: site, page, base, api)."""
    import json

    state = json.dumps({"site": site_view_, "page": page}, default=str, ensure_ascii=False).replace("<", "\\u003c")
    head = head_html(site_view_, page, canonical) + f"<script>window.__PB_DOCS__={state}</script>"
    out = template or FALLBACK
    out = out.replace("<!--pb-docs-head-->", head, 1) if "<!--pb-docs-head-->" in out else out.replace("</head>", head + "</head>", 1)
    return out.replace("<!--pb-docs-body-->", body_html(site_view_, page), 1)


# --- limits ---------------------------------------------------------------------------------------------------


class RateLimit:
    """Requests per client address in a sliding window, in this process."""

    def __init__(self, rate: int, window: float = 60.0) -> None:
        self.rate, self.window = rate, window
        self._seen: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        if len(self._seen) > 10000:  # many addresses: forget the quiet ones
            self._seen = {k: v for k, v in self._seen.items() if v and v[-1] > now - self.window}
        seen = self._seen.setdefault(key, deque())
        while seen and seen[0] <= now - self.window:
            seen.popleft()
        if len(seen) >= self.rate:
            return False
        seen.append(now)
        return True

