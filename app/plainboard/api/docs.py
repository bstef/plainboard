"""A project's doc sites in the app (#118, services/docs.py). Viewers read, members write, owners set the domain
(services/access.py). Live updates: ``doc_site.upserted`` (DocSiteOut), ``doc_site.deleted`` ({id}),
``doc_page.upserted`` (DocPageItem), ``doc_page.deleted`` ({id, site_id}), ``attachment.created`` for files."""

from __future__ import annotations

from fastapi import APIRouter, File, Request, UploadFile

from ..schemas import AttachmentOut, DocCloudflareIn, DocDnsOut, DocDomainIn, DocDomainOut, DocPageCreate, DocPageItem, DocPageOut, DocPageUpdate, DocSiteCreate, DocSiteOut, DocSiteUpdate, DocVerifyOut, DocVersionOut
from ..services import docs as docs_service
from ..services import docs_domains
from ..services import projects as project_service
from .deps import Session, Who
from .guard import _hosts

router = APIRouter(prefix="/api/projects/{project_id}/docs", tags=["docs"])


def _author(who) -> docs_service.Author:  # noqa: ANN001
    return docs_service.Author(who.name or "someone")


def _target(request: Request) -> str:
    """The host this PlainBoard was reached at: what a domain's CNAME points to."""
    hosts = sorted(_hosts(request.headers))
    forwarded = request.headers.get("x-forwarded-host", "").split(",")[0].strip().lower()
    return (forwarded or (hosts[0] if hosts else "")).split(":", 1)[0]


async def _page_out(session, page) -> DocPageOut:  # noqa: ANN001
    path = await docs_service.page_path(session, page)
    item = docs_service.item(page, path)
    return DocPageOut(**item.model_dump(), body=page.body, versions=await docs_service.count_versions(session, page.id))


@router.get("/sites", response_model=list[DocSiteOut])
async def list_sites(project_id: int, session: Session) -> list[DocSiteOut]:
    await project_service.get(session, project_id)
    return await docs_service.site_outs(session, await docs_service.sites(session, project_id))


@router.post("/sites", response_model=DocSiteOut, status_code=201)
async def create_site(project_id: int, body: DocSiteCreate, session: Session) -> DocSiteOut:
    await project_service.get(session, project_id)
    site = await docs_service.create_site(session, project_id, **body.model_dump())
    return (await docs_service.site_outs(session, [site]))[0]


@router.get("/sites/{site_id}", response_model=DocSiteOut)
async def get_site(project_id: int, site_id: int, session: Session) -> DocSiteOut:
    return (await docs_service.site_outs(session, [await docs_service.get_site(session, project_id, site_id)]))[0]


@router.patch("/sites/{site_id}", response_model=DocSiteOut)
async def update_site(project_id: int, site_id: int, body: DocSiteUpdate, session: Session) -> DocSiteOut:
    site = await docs_service.update_site(session, await docs_service.get_site(session, project_id, site_id), **body.model_dump(exclude_unset=True))
    return (await docs_service.site_outs(session, [site]))[0]


@router.delete("/sites/{site_id}", status_code=204)
async def delete_site(project_id: int, site_id: int, session: Session) -> None:
    await docs_service.delete_site(session, await docs_service.get_site(session, project_id, site_id))


@router.get("/sites/{site_id}/domain", response_model=DocDomainOut | None)
async def domain_setup(project_id: int, site_id: int, session: Session, request: Request) -> DocDomainOut | None:
    """What to set up at the DNS (or the tunnel) for the site's domain, and whether it is verified."""
    return docs_domains.setup(await docs_service.get_site(session, project_id, site_id), _target(request))


@router.put("/sites/{site_id}/domain", response_model=DocDomainOut | None)
async def set_domain(project_id: int, site_id: int, body: DocDomainIn, session: Session, request: Request) -> DocDomainOut | None:
    """The site's own domain ("" removes it); a new one has to be verified before the site is served there."""
    site = await docs_service.get_site(session, project_id, site_id)
    if body.domain.strip():
        await docs_domains.check_free(session, docs_service.clean_domain(body.domain), site.id, _hosts(request.headers))
    site = await docs_service.update_site(session, site, domain=body.domain)
    return docs_domains.setup(site, _target(request))


@router.post("/sites/{site_id}/domain/verify", response_model=DocVerifyOut)
async def verify_domain(project_id: int, site_id: int, session: Session) -> DocVerifyOut:
    return await docs_domains.verify(session, await docs_service.get_site(session, project_id, site_id))


@router.post("/sites/{site_id}/domain/cloudflare", response_model=DocDnsOut)
async def cloudflare_dns(project_id: int, site_id: int, body: DocCloudflareIn, session: Session, request: Request) -> DocDnsOut:
    """Creates (or changes) the proxied CNAME in Cloudflare; ``token`` is kept encrypted for the site, never shown."""
    site = await docs_service.get_site(session, project_id, site_id)
    if body.token is not None:
        await docs_domains.set_cloudflare_token(session, site, body.token)
    return await docs_domains.create_dns(session, site, body.target or _target(request), proxied=body.proxied)


@router.delete("/sites/{site_id}/domain/cloudflare", status_code=204)
async def forget_cloudflare(project_id: int, site_id: int, session: Session) -> None:
    await docs_domains.set_cloudflare_token(session, await docs_service.get_site(session, project_id, site_id), "")


@router.get("/sites/{site_id}/pages", response_model=list[DocPageItem])
async def list_pages(project_id: int, site_id: int, session: Session) -> list[DocPageItem]:
    """The tree in order (parents before children, siblings by position), without texts."""
    await docs_service.get_site(session, project_id, site_id)
    return await docs_service.page_items(session, site_id)


@router.post("/sites/{site_id}/pages", response_model=DocPageOut, status_code=201)
async def create_page(project_id: int, site_id: int, body: DocPageCreate, session: Session, who: Who) -> DocPageOut:
    site = await docs_service.get_site(session, project_id, site_id)
    page = await docs_service.create_page(session, site, _author(who), **body.model_dump())
    return await _page_out(session, page)


@router.get("/sites/{site_id}/files", response_model=list[AttachmentOut])
async def list_files(project_id: int, site_id: int, session: Session) -> list[AttachmentOut]:
    """Download and delete them as every attachment (/api/attachments/{id}); in a page: doc-file:<id>."""
    await docs_service.get_site(session, project_id, site_id)
    return [AttachmentOut.model_validate(a) for a in await docs_service.files(session, site_id)]


@router.post("/sites/{site_id}/files", response_model=list[AttachmentOut], status_code=201)
async def upload_files(project_id: int, site_id: int, session: Session, files: list[UploadFile] = File(...)) -> list[AttachmentOut]:
    site = await docs_service.get_site(session, project_id, site_id)
    out = []
    for file in files:
        out.append(AttachmentOut.model_validate(await docs_service.add_file(session, site, file.filename or "file", file.content_type or "", file.file)))
    return out


@router.get("/pages/{page_id}", response_model=DocPageOut)
async def get_page(project_id: int, page_id: int, session: Session) -> DocPageOut:
    return await _page_out(session, await docs_service.get_page(session, project_id, page_id))


@router.patch("/pages/{page_id}", response_model=DocPageOut)
async def update_page(project_id: int, page_id: int, body: DocPageUpdate, session: Session, who: Who) -> DocPageOut:
    page = await docs_service.get_page(session, project_id, page_id)
    page = await docs_service.update_page(session, page, _author(who), **body.model_dump(exclude_unset=True))
    return await _page_out(session, page)


@router.delete("/pages/{page_id}", status_code=204)
async def delete_page(project_id: int, page_id: int, session: Session) -> None:
    await docs_service.delete_page(session, await docs_service.get_page(session, project_id, page_id))


@router.get("/pages/{page_id}/versions", response_model=list[DocVersionOut])
async def list_versions(project_id: int, page_id: int, session: Session) -> list[DocVersionOut]:
    """Newest first, without their texts; the first is the page as it is."""
    page = await docs_service.get_page(session, project_id, page_id)
    return [DocVersionOut.model_validate(v).model_copy(update={"body": None}) for v in await docs_service.versions(session, page.id)]


@router.get("/pages/{page_id}/versions/{version_id}", response_model=DocVersionOut)
async def get_version(project_id: int, page_id: int, version_id: int, session: Session) -> DocVersionOut:
    page = await docs_service.get_page(session, project_id, page_id)
    return DocVersionOut.model_validate(await docs_service.get_version(session, page, version_id))


@router.post("/pages/{page_id}/versions/{version_id}/restore", response_model=DocPageOut)
async def restore_version(project_id: int, page_id: int, version_id: int, session: Session, who: Who) -> DocPageOut:
    page = await docs_service.get_page(session, project_id, page_id)
    return await _page_out(session, await docs_service.restore(session, page, version_id, _author(who)))


@router.post("/pages/{page_id}/confirm", response_model=DocPageOut)
async def confirm_page(project_id: int, page_id: int, session: Session, who: Who) -> DocPageOut:
    """Still right after what changed: the mark that it may be outdated goes, without a new version."""
    page = await docs_service.get_page(session, project_id, page_id)
    return await _page_out(session, await docs_service.confirm_page(session, page, _author(who), "confirmed"))
