"""Doc sites (#118): documentation that lives in PlainBoard, written by people and agents, kept current by agents.

A project has any number of sites. A site has a doc spec (what the user wants in it: content, structure, tone,
audience; written by the manager with the user, followed by every agent that writes the site) and pages in a tree.
Every saved state of a page is kept as a version (who, when, why) and can be restored. Files of a site are
attachments with ``doc_site_id``: the retention (services/retention.py) only ever touches files of done tickets and
the cleanup only workspaces, so they stay until their site goes or someone deletes them.

In a page, ``doc-file:<attachment id>`` names a file of the site and ``doc-page:<page id>`` another page
(``![Diagram](doc-file:12)``, ``[Setup](doc-page:3)``): links that survive moves, renames and the move of a project;
the renderer (services/docs_public.py) turns them into addresses. Published sites are served without login
(api/docs_public.py) at /docs/<slug>/ and, once verified, at their own domain (services/docs_domains.py).
What keeps pages current is orchestration/docs_watcher.py.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, BinaryIO

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..events import bus
from ..models import Attachment, DocPage, DocPageVersion, DocSite, utcnow
from ..schemas import AttachmentOut, DocPageItem, DocSiteOut
from . import board as board_service
from .errors import NotFound, ServiceError
from .util import slugify

UNSET: Any = object()
DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
FILE_REF = re.compile(r"doc-file:(\d+)")
PAGE_REF = re.compile(r"doc-page:(\d+)")


# --- sites ----------------------------------------------------------------------------------------------------


async def unique_site_slug(session: AsyncSession, name: str, exclude_id: int | None = None) -> str:
    base = slugify(name, "docs")[:70]
    slug, n = base, 2
    while (found := await session.scalar(select(DocSite.id).where(DocSite.slug == slug))) is not None and found != exclude_id:
        slug, n = f"{base}-{n}", n + 1
    return slug


def clean_domain(value: str) -> str:
    """A host name as typed (also as a URL) in lower case, or an error."""
    host = value.strip().lower()
    host = re.sub(r"^[a-z]+://", "", host).split("/", 1)[0].split(":", 1)[0].rstrip(".")
    if not DOMAIN.match(host) or host.endswith((".local", ".localhost", ".internal")):
        raise ServiceError("Not a domain name: give a host name like docs.example.com.")
    return host


def site_out(site: DocSite, pages: int = 0, stale: int = 0) -> DocSiteOut:
    return DocSiteOut(
        id=site.id, project_id=site.project_id, name=site.name, slug=site.slug, description=site.description, spec=site.spec,
        published=site.published, domain=site.domain, domain_verified_at=site.domain_verified_at,
        cloudflare_token_set=bool(site.cloudflare_token_enc), url=f"/docs/{site.slug}/", pages=pages, stale_pages=stale,
        created_at=site.created_at, updated_at=site.updated_at,
    )  # fmt: skip


async def site_outs(session: AsyncSession, sites: list[DocSite]) -> list[DocSiteOut]:
    if not sites:
        return []
    ids = [s.id for s in sites]
    rows = await session.execute(
        select(DocPage.site_id, func.count(), func.count(DocPage.flagged_at)).where(DocPage.site_id.in_(ids)).group_by(DocPage.site_id)
    )
    counts = {site_id: (int(n), int(stale)) for site_id, n, stale in rows.all()}
    return [site_out(s, *counts.get(s.id, (0, 0))) for s in sites]


async def publish_site(session: AsyncSession, site: DocSite) -> None:
    (out,) = await site_outs(session, [site])
    bus.publish(site.project_id, "doc_site.upserted", out)


async def sites(session: AsyncSession, project_id: int) -> list[DocSite]:
    return list(await session.scalars(select(DocSite).where(DocSite.project_id == project_id).order_by(DocSite.id)))


async def get_site(session: AsyncSession, project_id: int, site_id: int) -> DocSite:
    site = await session.get(DocSite, site_id)
    if site is None or site.project_id != project_id:
        raise NotFound("Doc site not found")
    return site


async def find_site(session: AsyncSession, project_id: int, ref: str) -> DocSite:
    """A site by its id, slug or name (for agents)."""
    ref = ref.strip()
    for site in await sites(session, project_id):
        if ref in (str(site.id), site.slug) or ref.lower() == site.name.lower():
            return site
    names = ", ".join(f"{s.slug} ({s.name})" for s in await sites(session, project_id)) or "none yet"
    raise NotFound(f"No doc site {ref!r}. Sites: {names}")


async def create_site(session: AsyncSession, project_id: int, *, name: str, slug: str | None = None, description: str = "", spec: str = "", published: bool = False) -> DocSite:
    if slug and await session.scalar(select(DocSite.id).where(DocSite.slug == slug)) is not None:
        raise ServiceError(f"The address /docs/{slug}/ is taken: choose another slug.", 409)
    site = DocSite(
        project_id=project_id, name=name.strip(), slug=slug or await unique_site_slug(session, name), description=description.strip(), spec=spec.strip(),
        published=published, domain_token=secrets.token_urlsafe(24),
    )  # fmt: skip
    session.add(site)
    await session.commit()
    await publish_site(session, site)
    return site


async def update_site(
    session: AsyncSession, site: DocSite, *, name: str | None = None, slug: str | None = None, description: str | None = None,
    spec: str | None = None, published: bool | None = None, domain: str | None = None,
) -> DocSite:  # fmt: skip
    """``domain``: "" removes it; another one has to be verified again."""
    if slug is not None and slug != site.slug:
        if await session.scalar(select(DocSite.id).where(DocSite.slug == slug, DocSite.id != site.id)) is not None:
            raise ServiceError(f"The address /docs/{slug}/ is taken: choose another slug.", 409)
        site.slug = slug
    if name is not None:
        site.name = name.strip()
    if description is not None:
        site.description = description.strip()
    if spec is not None:
        site.spec = spec.strip()
    if published is not None:
        site.published = published
    if domain is not None:
        new = clean_domain(domain) if domain.strip() else None
        if new != site.domain:
            from . import docs_domains

            if new is not None:
                await docs_domains.check_free(session, new, site.id)
            site.domain, site.domain_verified_at = new, None
            site.domain_token = secrets.token_urlsafe(24)
            docs_domains.forget()
    await session.commit()
    await publish_site(session, site)
    return site


async def delete_site(session: AsyncSession, site: DocSite) -> None:
    """With its pages, their versions and its files (on disk too)."""
    project_id, site_id = site.project_id, site.id
    for attachment in await files(session, site_id):
        board_service.storage_path(attachment).unlink(missing_ok=True)
    await session.delete(site)
    await session.commit()
    from . import docs_domains

    docs_domains.forget()
    bus.publish(project_id, "doc_site.deleted", {"id": site_id})


# --- pages ----------------------------------------------------------------------------------------------------


async def pages(session: AsyncSession, site_id: int) -> list[DocPage]:
    """All pages of a site, in tree order (parents before their children, siblings by position)."""
    rows = list(await session.scalars(select(DocPage).where(DocPage.site_id == site_id).order_by(DocPage.position, DocPage.id)))
    return ordered(rows)


def ordered(rows: list[DocPage]) -> list[DocPage]:
    children: dict[int | None, list[DocPage]] = {}
    for page in sorted(rows, key=lambda p: (p.position, p.id)):
        children.setdefault(page.parent_id, []).append(page)
    out: list[DocPage] = []

    def walk(parent: int | None) -> None:
        for page in children.get(parent, []):
            out.append(page)
            walk(page.id)

    walk(None)
    return out


def url_paths(rows: Iterable[DocPage]) -> dict[int, str]:
    """Each page's address under its site: the slugs from the top down (parent/child)."""
    by_id = {p.id: p for p in rows}
    out: dict[int, str] = {}

    def path(page: DocPage, depth: int = 0) -> str:
        if page.id in out:
            return out[page.id]
        parent = by_id.get(page.parent_id) if page.parent_id else None
        out[page.id] = f"{path(parent, depth + 1)}/{page.slug}" if parent is not None and depth < 20 else page.slug
        return out[page.id]

    for page in by_id.values():
        path(page)
    return out


def depth(page: DocPage, by_id: dict[int, DocPage]) -> int:
    n, at = 0, page
    while at.parent_id and at.parent_id in by_id and n < 20:
        n, at = n + 1, by_id[at.parent_id]
    return n


def item(page: DocPage, path: str) -> DocPageItem:
    return DocPageItem(
        id=page.id, site_id=page.site_id, parent_id=page.parent_id, position=page.position, title=page.title, slug=page.slug, path=path,
        paths=list(page.paths or []), edited_by=page.edited_by, edited_at=page.edited_at, drift=page.drift, flagged_at=page.flagged_at,
        flag_reason=page.flag_reason, updated_at=page.updated_at,
    )  # fmt: skip


async def page_items(session: AsyncSession, site_id: int) -> list[DocPageItem]:
    rows = await pages(session, site_id)
    paths = url_paths(rows)
    return [item(p, paths[p.id]) for p in rows]


async def get_page(session: AsyncSession, project_id: int, page_id: int) -> DocPage:
    page = await session.get(DocPage, page_id)
    if page is None or page.project_id != project_id:
        raise NotFound(f"Doc page {page_id} not found")
    return page


async def page_path(session: AsyncSession, page: DocPage) -> str:
    return url_paths(await pages(session, page.site_id))[page.id]


async def publish_page(session: AsyncSession, page: DocPage) -> None:
    bus.publish(page.project_id, "doc_page.upserted", item(page, await page_path(session, page)))


async def _unique_page_slug(session: AsyncSession, site_id: int, parent_id: int | None, base: str, exclude_id: int | None) -> str:
    q = select(DocPage.slug).where(DocPage.site_id == site_id, DocPage.parent_id.is_(None) if parent_id is None else DocPage.parent_id == parent_id)
    if exclude_id is not None:
        q = q.where(DocPage.id != exclude_id)
    taken = set(await session.scalars(q))
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


async def _place(session: AsyncSession, page: DocPage, parent_id: int | None, position: int | None) -> None:
    """Put the page among its new siblings at ``position`` (None: last) and number them anew."""
    if parent_id is not None:
        parent = await session.get(DocPage, parent_id)
        if parent is None or parent.site_id != page.site_id:
            raise ServiceError("The parent page is not a page of this site.")
        at, seen = parent, 0
        while at is not None and seen < 50:  # not below itself
            if page.id is not None and at.id == page.id:
                raise ServiceError("A page cannot be moved below itself.")
            at, seen = (await session.get(DocPage, at.parent_id) if at.parent_id else None), seen + 1
    siblings = list(
        await session.scalars(
            select(DocPage)
            .where(DocPage.site_id == page.site_id, DocPage.parent_id.is_(None) if parent_id is None else DocPage.parent_id == parent_id)
            .order_by(DocPage.position, DocPage.id)
        )
    )
    siblings = [p for p in siblings if p is not page and (page.id is None or p.id != page.id)]
    at = len(siblings) if position is None else min(position, len(siblings))
    siblings.insert(at, page)
    page.parent_id = parent_id
    for n, sibling in enumerate(siblings):
        sibling.position = n


@dataclass
class Author:
    name: str
    agent_id: int | None = None


async def create_page(
    session: AsyncSession, site: DocSite, author: Author, *, title: str, body: str = "", parent_id: int | None = None, position: int | None = None,
    slug: str | None = None, paths: list[str] | None = None, reason: str = "",
) -> DocPage:  # fmt: skip
    now = utcnow()
    page = DocPage(project_id=site.project_id, site_id=site.id, title=title.strip()[:200], body=body, paths=_paths(paths or []), edited_by=author.name, edited_at=now)
    await _place(session, page, parent_id, position)
    page.slug = await _unique_page_slug(session, site.id, parent_id, slug or slugify(title, "page"), None)
    session.add(page)
    await session.flush()
    session.add(DocPageVersion(project_id=site.project_id, page_id=page.id, title=page.title, body=body, author=author.name, agent_id=author.agent_id, reason=(reason or "created")[:300], created_at=now))
    site.updated_at = now
    await session.commit()
    await publish_page(session, page)
    return page


def _paths(values: list[str]) -> list[str]:
    return list(dict.fromkeys(v.strip().lstrip("./").strip() for v in values if v.strip()))[:50]


async def update_page(
    session: AsyncSession, page: DocPage, author: Author, *, title: str | None = None, body: str | None = None, move: bool = False,
    parent_id: int | None = None, position: int | None = None, slug: str | None = None, paths: list[str] | None = None, reason: str = "",
) -> DocPage:  # fmt: skip
    """A new title or text is a new version; a move, a new slug or new linked paths are none."""
    now = utcnow()
    if move or position is not None:
        target = parent_id if move else page.parent_id
        await _place(session, page, target, position)
        if slug is None:  # its slug stays unless a sibling there has it
            page.slug = await _unique_page_slug(session, page.site_id, target, page.slug, page.id)
    if slug is not None and slug != page.slug:
        page.slug = await _unique_page_slug(session, page.site_id, page.parent_id, slug, page.id)
    if paths is not None:
        page.paths = _paths(paths)
    changed = (title is not None and title.strip() != page.title) or (body is not None and body != page.body)
    if changed:
        page.title = title.strip()[:200] if title is not None else page.title
        page.body = body if body is not None else page.body
        page.edited_by, page.edited_at = author.name, now
        page.drift, page.flagged_at, page.flag_reason, page.reminded_at = 0, None, "", None  # it was looked at
        session.add(DocPageVersion(project_id=page.project_id, page_id=page.id, title=page.title, body=page.body, author=author.name, agent_id=author.agent_id, reason=reason[:300], created_at=now))
    page.updated_at = now
    await session.execute(update(DocSite).where(DocSite.id == page.site_id).values(updated_at=now))
    await session.commit()
    await publish_page(session, page)
    return page


async def confirm_page(session: AsyncSession, page: DocPage, author: Author, reason: str) -> DocPage:
    """Looked at and still right: the mark that it may be outdated goes, without a new version."""
    page.drift, page.flagged_at, page.flag_reason, page.reminded_at = 0, None, "", None
    page.edited_at = utcnow()
    await session.commit()
    await publish_page(session, page)
    return page


async def delete_page(session: AsyncSession, page: DocPage) -> None:
    """With the pages below it and all their versions."""
    project_id, page_id, site_id = page.project_id, page.id, page.site_id
    await session.delete(page)
    await session.commit()
    bus.publish(project_id, "doc_page.deleted", {"id": page_id, "site_id": site_id})


# --- versions -------------------------------------------------------------------------------------------------


async def versions(session: AsyncSession, page_id: int, limit: int = 100) -> list[DocPageVersion]:
    q = select(DocPageVersion).where(DocPageVersion.page_id == page_id).order_by(DocPageVersion.id.desc()).limit(limit)
    return list(await session.scalars(q))


async def count_versions(session: AsyncSession, page_id: int) -> int:
    return int(await session.scalar(select(func.count()).select_from(DocPageVersion).where(DocPageVersion.page_id == page_id)) or 0)


async def get_version(session: AsyncSession, page: DocPage, version_id: int) -> DocPageVersion:
    version = await session.get(DocPageVersion, version_id)
    if version is None or version.page_id != page.id:
        raise NotFound("Version not found")
    return version


async def restore(session: AsyncSession, page: DocPage, version_id: int, author: Author) -> DocPage:
    version = await get_version(session, page, version_id)
    when = f"{version.created_at:%Y-%m-%d %H:%M}"
    return await update_page(session, page, author, title=version.title, body=version.body, reason=f"restored the version of {when} by {version.author}")


# --- files ----------------------------------------------------------------------------------------------------


async def add_file(session: AsyncSession, site: DocSite, filename: str, content_type: str, data: bytes | BinaryIO, *, agent_id: int | None = None) -> Attachment:
    attachment = await board_service.store_attachment(session, site.project_id, filename, content_type, data, doc_site_id=site.id, agent_id=agent_id)
    bus.publish(site.project_id, "attachment.created", AttachmentOut.model_validate(attachment))
    return attachment


async def files(session: AsyncSession, site_id: int) -> list[Attachment]:
    return list(await session.scalars(select(Attachment).where(Attachment.doc_site_id == site_id).order_by(Attachment.id)))


async def get_file(session: AsyncSession, site: DocSite, attachment_id: int) -> Attachment:
    attachment = await session.get(Attachment, attachment_id)
    if attachment is None or attachment.doc_site_id != site.id:
        raise NotFound("File not found")
    return attachment


def file_markdown(attachment: Attachment) -> str:
    image = attachment.content_type.startswith("image/")
    return f"{'!' if image else ''}[{attachment.filename}](doc-file:{attachment.id})"


# --- moving a project (services/transfer.py) --------------------------------------------------------------------


def _relink(text: str, files: dict[int, int], pages: dict[int, int]) -> str:
    text = FILE_REF.sub(lambda m: f"doc-file:{files.get(int(m.group(1)), int(m.group(1)))}", text)
    return PAGE_REF.sub(lambda m: f"doc-page:{pages.get(int(m.group(1)), int(m.group(1)))}", text)


async def after_import(session: AsyncSession, new: dict[str, dict[int, int]]) -> None:
    """Imported pages name their files and pages by the ids of the other side: by the new ones from now on."""
    files, page_ids = new.get("attachments", {}), new.get("doc_pages", {})
    if not page_ids or not (files or page_ids):
        return
    for page in await session.scalars(select(DocPage).where(DocPage.id.in_(list(page_ids.values())))):
        page.body = _relink(page.body, files, page_ids)
    for version in await session.scalars(select(DocPageVersion).where(DocPageVersion.page_id.in_(list(page_ids.values())))):
        version.body = _relink(version.body, files, page_ids)
