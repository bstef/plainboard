"""Doc sites (#118, services/docs.py): agents read and write pages, the manager makes sites and keeps their doc spec.

The doc spec says what the user wants in a site; every agent that writes it gets it with what it reads, and with its
first write of the site in a turn if it read nothing before.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..models import DocPage, DocSite
from ..services import docs as docs_service
from ..services.util import truncate
from .registry import ALL, DOCS, MANAGER, ToolContext, ToolError, local_tool, tool

_spec_seen: OrderedDict[tuple[int, int], None] = OrderedDict()  # (run, site): the spec was in a result of this turn


def _saw(run_id: int, site_id: int) -> bool:
    key = (run_id, site_id)
    seen = key in _spec_seen
    _spec_seen[key] = None
    while len(_spec_seen) > 2000:
        _spec_seen.popitem(last=False)
    return seen


def _spec(site: DocSite) -> str:
    if not site.spec.strip():
        return f"Doc spec of {site.slug}: none yet (the manager writes it with the user)."
    return f"Doc spec of {site.slug} (follow it):\n{site.spec.strip()}"


def _author(ctx: ToolContext) -> docs_service.Author:
    return docs_service.Author(ctx.agent.name, ctx.agent.id)


def _site_line(site, pages: int, stale: int) -> str:  # noqa: ANN001
    where = f"/docs/{site.slug}/" + (f", {site.domain}" + ("" if site.domain_verified_at else " (not verified)") if site.domain else "")
    served = ("public" if site.public else "private") if site.published else "draft"
    langs = f"; languages {site.language}+{','.join(site.languages)}" if site.languages else ""
    return f"- {site.slug}: {site.name} [{served}; {where}; {pages} pages" + (f", {stale} marked outdated" if stale else "") + f"{langs}]"


LISTED = 50  # pages per call
MARKED = 15


def _page_line(p: DocPage, children: int) -> str:
    mark = f" [marked outdated: {truncate(p.flag_reason, 160)}]" if p.flagged_at else ""
    linked = f" paths: {', '.join(p.paths)}" if p.paths else ""
    below = f" ({children} below)" if children else ""
    return f"- {p.id} /{p.path} \"{p.title}\"{below} (edited {p.edited_at:%Y-%m-%d} by {p.edited_by}){linked}{mark}"


class ListDocsArgs(BaseModel):
    site: str | None = Field(default=None, description="Site slug/id: its doc spec and pages; empty = all sites")
    parent: int | None = Field(default=None, description="Pages below this page id; empty = the top level")
    query: str | None = Field(default=None, max_length=200, description="Find pages by words in title or address instead")
    marked: bool = Field(default=False, description="Only pages marked as possibly outdated")
    offset: int = Field(default=0, ge=0)


@tool(
    "list_docs",
    "The project's doc sites, or one site's doc spec and pages: one level of the tree (parent), a search (query) or the marked ones, 50 at a time.",
    ListDocsArgs,
    roles=ALL,
    feature=DOCS,
)
async def list_docs(ctx: ToolContext, args: ListDocsArgs) -> str:
    if not args.site:
        outs = await docs_service.site_outs(ctx.session, await docs_service.sites(ctx.session, ctx.project.id))
        if not outs:
            return "No doc sites yet."
        return "\n".join(_site_line(s, s.pages, s.stale_pages) for s in outs)
    site = await docs_service.find_site(ctx.session, ctx.project.id, args.site)
    head = [] if _saw(ctx.run.run_id, site.id) else [_spec(site), ""]
    if args.query:
        rows = await docs_service.search_titles(ctx.session, site.id, args.query, LISTED)
        title, total = f"Pages of {site.slug} matching {args.query!r}:", len(rows)
    elif args.marked:
        q = select(DocPage).where(DocPage.site_id == site.id, DocPage.flagged_at.is_not(None))
        total = int(await ctx.session.scalar(select(func.count()).select_from(q.subquery())) or 0)
        rows = list(await ctx.session.scalars(q.order_by(DocPage.flagged_at).offset(args.offset).limit(MARKED)))
        title = f"Pages of {site.slug} marked as possibly outdated ({total}):"
    else:
        rows, _, total = await docs_service.children(ctx.session, site.id, args.parent, args.offset, LISTED)
        where = f"below page {args.parent}" if args.parent else "at the top"
        title = f"Pages of {site.slug} {where} ({args.offset + 1}-{args.offset + len(rows)} of {total}):" if rows else f"No pages {where}."
    counts = await docs_service.child_counts(ctx.session, [p.id for p in rows])
    lines = head + [title] + [_page_line(p, counts.get(p.id, 0)) for p in rows]
    if not args.query and args.offset + len(rows) < total:
        lines.append(f"(more: offset={args.offset + len(rows)})")
    return "\n".join(lines)


class ReadDocPageArgs(BaseModel):
    page: int = Field(description="Page id")
    offset: int = Field(default=0, ge=0, description="Characters to skip of a long page")


READ_CHARS = 20000


@tool("read_doc_page", "A doc page's Markdown (long pages in parts), with its site's doc spec.", ReadDocPageArgs, roles=ALL, feature=DOCS)
async def read_doc_page(ctx: ToolContext, args: ReadDocPageArgs) -> str:
    page = await docs_service.get_page(ctx.session, ctx.project.id, args.page)
    site = await ctx.session.get(DocSite, page.site_id)
    assert site is not None
    spec = "" if _saw(ctx.run.run_id, site.id) else _spec(site) + "\n\n"
    facts = [f"site {site.slug}", f"/{page.path}", f"edited {page.edited_at:%Y-%m-%d} by {page.edited_by}"]
    if page.paths:
        facts.append(f"paths: {', '.join(page.paths)}")
    mark = f"\nMarked as possibly outdated: {page.flag_reason}" if page.flagged_at else ""
    body = page.body or "(empty)"
    part = body[args.offset : args.offset + READ_CHARS]
    more = f"\n\n(characters {args.offset}-{args.offset + len(part)} of {len(body)}; more: offset={args.offset + len(part)})" if args.offset or len(body) > args.offset + READ_CHARS else ""
    return f"{spec}Doc page {page.id} \"{page.title}\" ({'; '.join(facts)}){mark}\n\n{part}{more}"


class WriteDocPageArgs(BaseModel):
    reason: str = Field(min_length=3, max_length=300, description="Why, in one short sentence (kept with the version)")
    page: int | None = Field(default=None, description="Page id to change; empty = a new page")
    site: str | None = Field(default=None, description="New page: site slug/id")
    title: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, description="Whole Markdown text. Files: ![alt](doc-file:ID), links to pages: [text](doc-page:ID)")
    parent: int | None = Field(default=None, description="Parent page id (with move: 0 = top level)")
    position: int | None = Field(default=None, ge=0, description="Place among its siblings, 0 = first")
    move: bool = Field(default=False, description="Move the page to parent/position")
    slug: str | None = Field(default=None, max_length=80, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    paths: list[str] | None = Field(default=None, max_length=50, description="Code paths it describes (prefix or glob): changes there mark it for update")
    still_current: bool = Field(default=False, description="Checked after a change: still right, clear its outdated mark")


@tool(
    "write_doc_page",
    "Create, change or move a doc page; every change of title or text is kept as a version. Follow the site's doc spec.",
    WriteDocPageArgs,
    roles=ALL,
    feature=DOCS,
)
async def write_doc_page(ctx: ToolContext, args: WriteDocPageArgs) -> str:
    author = _author(ctx)
    if args.page is None:
        if not args.site or not args.title:
            raise ToolError("A new page needs site and title.")
        site = await docs_service.find_site(ctx.session, ctx.project.id, args.site)
        page = await docs_service.create_page(
            ctx.session, site, author, title=args.title, body=args.body or "", parent_id=args.parent or None, position=args.position,
            slug=args.slug, paths=args.paths, reason=args.reason,
        )  # fmt: skip
        done = f"Created doc page {page.id} /{page.path} in {site.slug}."
    else:
        page = await docs_service.get_page(ctx.session, ctx.project.id, args.page)
        site = await ctx.session.get(DocSite, page.site_id)
        assert site is not None
        if args.still_current and args.body is None and args.title is None:
            await docs_service.confirm_page(ctx.session, page, author, args.reason)
            return f"Doc page {page.id} confirmed as current."
        versions = await docs_service.count_versions(ctx.session, page.id)
        page = await docs_service.update_page(
            ctx.session, page, author, title=args.title, body=args.body, move=args.move, parent_id=args.parent or None, position=args.position,
            slug=args.slug, paths=args.paths, reason=args.reason,
        )  # fmt: skip
        changed = await docs_service.count_versions(ctx.session, page.id) > versions
        done = f"Doc page {page.id} /{page.path} {'saved as a new version' if changed else 'updated (text unchanged)'}."
    if not _saw(ctx.run.run_id, site.id):
        done += f"\n\n{_spec(site)}\nIf the page does not follow it yet, change it."
    return done


class ManageDocSiteArgs(BaseModel):
    site: str | None = Field(default=None, description="Site slug/id to change; empty = a new site")
    name: str | None = Field(default=None, max_length=200)
    slug: str | None = Field(default=None, max_length=80, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", description="Its address /docs/<slug>/")
    description: str | None = Field(default=None, max_length=300, description="One sentence for search engines")
    spec: str | None = Field(default=None, max_length=20000, description="Doc spec agreed with the user: content, structure, tone, audience (Markdown, whole text)")
    published: bool | None = Field(default=None, description="Served at all (else a draft); only when the user wants it")
    public: bool | None = Field(default=None, description="Served to everyone without login; else only to people of this project")
    languages: list[str] | None = Field(default=None, max_length=30, description="Further languages (codes), translated automatically")
    translator: Literal["", "llm", "deepl", "google"] | None = Field(default=None, description="How (deepl/google need the API key the user enters in the site's settings)")
    translate_model: str | None = Field(default=None, max_length=100, description="llm: model id; empty = the provider's small one")


@tool("manage_doc_site", "Create a doc site or change its name, slug, description, doc spec or published state.", ManageDocSiteArgs, roles=(MANAGER,))
async def manage_doc_site(ctx: ToolContext, args: ManageDocSiteArgs) -> str:
    if args.site is None:
        if not args.name:
            raise ToolError("A new site needs a name.")
        site = await docs_service.create_site(
            ctx.session, ctx.project.id, name=args.name, slug=args.slug, description=args.description or "", spec=args.spec or "", published=bool(args.published),
            public=bool(args.public), languages=args.languages or [],
        )
        if args.translator or args.translate_model:
            site = await docs_service.update_site(ctx.session, site, translator=args.translator, translate_model=args.translate_model)
        hint = "" if site.spec else " Write its doc spec with the user before pages are written."
        served = ("public" if site.public else "private") if site.published else "draft"
        return f"Created doc site {site.slug} (id {site.id}, /docs/{site.slug}/, {served}).{hint}"
    site = await docs_service.find_site(ctx.session, ctx.project.id, args.site)
    site = await docs_service.update_site(ctx.session, site, **args.model_dump(exclude={"site"}, exclude_none=True))
    served = ("public" if site.public else "private") if site.published else "draft"
    return f"Doc site {site.slug} saved ({served}{'; languages ' + ', '.join(site.languages) if site.languages else ''})."


class AttachDocFileArgs(BaseModel):
    site: str = Field(description="Site slug/id")
    path: str = Field(description="File in your workspace (absolute or relative)")


local_tool("attach_doc_file", "Add a file or image from your workspace to a doc site; kept for good. Returns how to use it in a page.", AttachDocFileArgs, feature=DOCS)

