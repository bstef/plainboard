"""Doc sites (#118, services/docs.py): agents read and write pages, the manager makes sites and keeps their doc spec.

The doc spec says what the user wants in a site; every agent that writes it gets it with what it reads, and with its
first write of the site in a turn if it read nothing before.
"""

from __future__ import annotations

from collections import OrderedDict

from pydantic import BaseModel, Field

from ..models import DocSite
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


def _site_line(site: DocSite, pages: int, stale: int) -> str:
    where = f"/docs/{site.slug}/" + (f", {site.domain}" + ("" if site.domain_verified_at else " (not verified)") if site.domain else "")
    return f"- {site.slug}: {site.name} [{'published' if site.published else 'draft'}; {where}; {pages} pages" + (f", {stale} marked outdated" if stale else "") + "]"


class ListDocsArgs(BaseModel):
    site: str | None = Field(default=None, description="Site slug/id: its doc spec and page tree; empty = all sites")


@tool("list_docs", "The project's doc sites, or one site's doc spec and page tree (ids, paths, marks).", ListDocsArgs, roles=ALL, feature=DOCS)
async def list_docs(ctx: ToolContext, args: ListDocsArgs) -> str:
    if not args.site:
        outs = await docs_service.site_outs(ctx.session, await docs_service.sites(ctx.session, ctx.project.id))
        if not outs:
            return "No doc sites yet."
        return "\n".join(_site_line(s, s.pages, s.stale_pages) for s in outs)  # type: ignore[arg-type]
    site = await docs_service.find_site(ctx.session, ctx.project.id, args.site)
    _saw(ctx.run.run_id, site.id)
    rows = await docs_service.pages(ctx.session, site.id)
    paths = docs_service.url_paths(rows)
    by_id = {p.id: p for p in rows}
    lines = [_spec(site), "", f"Pages of {site.slug} ({len(rows)}):" if rows else "No pages yet."]
    for p in rows:
        mark = f" [marked outdated: {truncate(p.flag_reason, 160)}]" if p.flagged_at else ""
        linked = f" paths: {', '.join(p.paths)}" if p.paths else ""
        lines.append(f"{'  ' * docs_service.depth(p, by_id)}- {p.id} /{paths[p.id]} \"{p.title}\" (edited {p.edited_at:%Y-%m-%d} by {p.edited_by}){linked}{mark}")
    return "\n".join(lines)


class ReadDocPageArgs(BaseModel):
    page: int = Field(description="Page id")


@tool("read_doc_page", "A doc page's Markdown, with its site's doc spec.", ReadDocPageArgs, roles=ALL, feature=DOCS)
async def read_doc_page(ctx: ToolContext, args: ReadDocPageArgs) -> str:
    page = await docs_service.get_page(ctx.session, ctx.project.id, args.page)
    site = await ctx.session.get(DocSite, page.site_id)
    assert site is not None
    spec = "" if _saw(ctx.run.run_id, site.id) else _spec(site) + "\n\n"
    facts = [f"site {site.slug}", f"/{await docs_service.page_path(ctx.session, page)}", f"edited {page.edited_at:%Y-%m-%d} by {page.edited_by}"]
    if page.paths:
        facts.append(f"paths: {', '.join(page.paths)}")
    mark = f"\nMarked as possibly outdated: {page.flag_reason}" if page.flagged_at else ""
    return f"{spec}Doc page {page.id} \"{page.title}\" ({'; '.join(facts)}){mark}\n\n{page.body or '(empty)'}"


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
        done = f"Created doc page {page.id} /{await docs_service.page_path(ctx.session, page)} in {site.slug}."
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
        done = f"Doc page {page.id} /{await docs_service.page_path(ctx.session, page)} {'saved as a new version' if changed else 'updated (text unchanged)'}."
    if not _saw(ctx.run.run_id, site.id):
        done += f"\n\n{_spec(site)}\nIf the page does not follow it yet, change it."
    return done


class ManageDocSiteArgs(BaseModel):
    site: str | None = Field(default=None, description="Site slug/id to change; empty = a new site")
    name: str | None = Field(default=None, max_length=200)
    slug: str | None = Field(default=None, max_length=80, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", description="Its address /docs/<slug>/")
    description: str | None = Field(default=None, max_length=300, description="One sentence for search engines")
    spec: str | None = Field(default=None, max_length=20000, description="Doc spec agreed with the user: content, structure, tone, audience (Markdown, whole text)")
    published: bool | None = Field(default=None, description="Public without login (only when the user wants it)")


@tool("manage_doc_site", "Create a doc site or change its name, slug, description, doc spec or published state.", ManageDocSiteArgs, roles=(MANAGER,))
async def manage_doc_site(ctx: ToolContext, args: ManageDocSiteArgs) -> str:
    if args.site is None:
        if not args.name:
            raise ToolError("A new site needs a name.")
        site = await docs_service.create_site(
            ctx.session, ctx.project.id, name=args.name, slug=args.slug, description=args.description or "", spec=args.spec or "", published=bool(args.published)
        )
        hint = "" if site.spec else " Write its doc spec with the user before pages are written."
        return f"Created doc site {site.slug} (id {site.id}, /docs/{site.slug}/, {'published' if site.published else 'draft'}).{hint}"
    site = await docs_service.find_site(ctx.session, ctx.project.id, args.site)
    site = await docs_service.update_site(ctx.session, site, name=args.name, slug=args.slug, description=args.description, spec=args.spec, published=args.published)
    return f"Doc site {site.slug} saved ({'published' if site.published else 'draft'})."


class AttachDocFileArgs(BaseModel):
    site: str = Field(description="Site slug/id")
    path: str = Field(description="File in your workspace (absolute or relative)")


local_tool("attach_doc_file", "Add a file or image from your workspace to a doc site; kept for good. Returns how to use it in a page.", AttachDocFileArgs, feature=DOCS)

