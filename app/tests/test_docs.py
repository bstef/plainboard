"""Doc sites (#118): pages with versions, files that stay, agent tools, public serving, own domains, keeping docs current."""

import json
from datetime import timedelta

import httpx
import pytest
from conftest import POSTGRES, agents_by_slug, idle, wait_for
from plainboard_protocol import GitCommit
from test_flow import chat, make_member, manager_of, runner, tool_results
from test_permissions import as_friend, friend, sign_in

from conftest import ADMIN_PASSWORD


def site(client, pid: int, **body) -> dict:
    resp = client.post(f"/api/projects/{pid}/docs/sites", json={"name": "Handbook", **body})
    assert resp.status_code == 201, resp.text
    return resp.json()


def page(client, pid: int, site_id: int, **body) -> dict:
    resp = client.post(f"/api/projects/{pid}/docs/sites/{site_id}/pages", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def db(client, fn):
    from plainboard.db import sessionmaker

    async def run():
        async with sessionmaker()() as s:
            return await fn(s)

    return client.portal.call(run)


def test_pages_form_a_tree_keep_every_version_and_restore_one(client, project):
    pid = project["id"]
    s = site(client, pid, spec="For admins. Short pages, English.")
    assert s["slug"] == "handbook" and s["url"] == "/docs/handbook/" and not s["published"]
    start = page(client, pid, s["id"], title="Getting started", body="# Install\n\nRun it.", reason="first page")
    setup = page(client, pid, s["id"], title="Setup", parent_id=start["id"])
    again = page(client, pid, s["id"], title="Setup", parent_id=start["id"])
    assert (setup["path"], again["path"]) == ("getting-started/setup", "getting-started/setup-2")
    first = page(client, pid, s["id"], title="Overview", position=0)
    tree = client.get(f"/api/projects/{pid}/docs/sites/{s['id']}/pages").json()
    assert [p["title"] for p in tree] == ["Overview", "Getting started", "Setup", "Setup"]  # parents before children, by position

    # A move is no version; a text change is one, with who and why.
    moved = client.patch(f"/api/projects/{pid}/docs/pages/{again['id']}", json={"move": True, "parent_id": None, "position": 0}).json()
    assert moved["path"] == "setup-2" and moved["versions"] == 1  # its slug stays
    assert client.patch(f"/api/projects/{pid}/docs/pages/{start['id']}", json={"move": True, "parent_id": setup["id"]}).status_code == 400  # not below itself
    changed = client.patch(f"/api/projects/{pid}/docs/pages/{start['id']}", json={"body": "# Install\n\nRun `pb up`.", "reason": "new command"}).json()
    assert changed["versions"] == 2 and changed["edited_by"] == "Admin"
    versions = client.get(f"/api/projects/{pid}/docs/pages/{start['id']}/versions").json()
    assert [(v["reason"], v["body"]) for v in versions] == [("new command", None), ("first page", None)]
    restored = client.post(f"/api/projects/{pid}/docs/pages/{start['id']}/versions/{versions[1]['id']}/restore").json()
    assert restored["body"] == "# Install\n\nRun it." and restored["versions"] == 3
    assert client.get(f"/api/projects/{pid}/docs/pages/{start['id']}/versions").json()[0]["reason"].startswith("restored the version of")

    assert client.delete(f"/api/projects/{pid}/docs/pages/{start['id']}").status_code == 204  # with the page below it
    assert [p["id"] for p in client.get(f"/api/projects/{pid}/docs/sites/{s['id']}/pages").json()] == [again["id"], first["id"]]
    assert client.post(f"/api/projects/{pid}/docs/sites", json={"name": "Other", "slug": "handbook"}).status_code == 409


def test_viewers_read_members_write_and_only_the_owner_sets_the_domain(client, project):
    pid = project["id"]
    s = site(client, pid)
    olga, ben = friend(client, "olga"), friend(client, "ben")
    assert client.put(f"/api/projects/{pid}/members/{olga['id']}", json={"role": "viewer"}).status_code == 200
    assert client.put(f"/api/projects/{pid}/members/{ben['id']}", json={"role": "member"}).status_code == 200
    as_friend(client, "olga")
    assert client.get(f"/api/projects/{pid}/docs/sites").status_code == 200
    assert client.post(f"/api/projects/{pid}/docs/sites/{s['id']}/pages", json={"title": "X"}).status_code == 403
    as_friend(client, "ben")
    assert client.post(f"/api/projects/{pid}/docs/sites/{s['id']}/pages", json={"title": "X"}).status_code == 201
    assert client.put(f"/api/projects/{pid}/docs/sites/{s['id']}/domain", json={"domain": "docs.example.com"}).status_code == 403
    sign_in(client, "admin", ADMIN_PASSWORD)


def test_doc_files_are_never_deleted_by_the_retention(client, project):
    from plainboard.config import get_config
    from plainboard.models import Attachment, TaskTransition, utcnow
    from plainboard.services import retention
    from sqlalchemy import update

    pid = project["id"]
    s = site(client, pid)
    (picture,) = client.post(f"/api/projects/{pid}/docs/sites/{s['id']}/files", files=[("files", ("arch.png", b"\x89PNG\r\n\x1a\nxx", "image/png"))]).json()
    task = client.post(f"/api/projects/{pid}/tasks", json={"title": "Old"}).json()
    (old,) = client.post(f"/api/tasks/{task['id']}/attachments", files=[("files", ("old.png", b"x", "image/png"))]).json()
    client.patch(f"/api/tasks/{task['id']}", json={"status": "done"})
    assert client.patch(f"/api/projects/{pid}", json={"attachment_retention_days": 1}).status_code == 200

    async def purge(s):
        await s.execute(update(TaskTransition).where(TaskTransition.project_id == pid).values(at=utcnow() - timedelta(days=30)))
        await s.commit()
        await s.execute(update(Attachment).where(Attachment.id == picture["id"]).values(created_at=utcnow() - timedelta(days=4000)))
        await s.commit()
        done = await retention.purge(s, utcnow())
        return [d.attachment.id for d in done.get(pid, [])], (await s.get(Attachment, picture["id"])).storage_name

    purged, name = db(client, purge)
    assert purged == [old["id"]]
    assert (get_config().uploads_dir / name).is_file()
    assert [f["id"] for f in client.get(f"/api/projects/{pid}/docs/sites/{s['id']}/files").json()] == [picture["id"]]


def test_a_published_site_is_served_without_login_as_html_json_and_search(client, project, monkeypatch):
    pid = project["id"]
    s = site(client, pid, description="How to run the shop")
    (picture,) = client.post(f"/api/projects/{pid}/docs/sites/{s['id']}/files", files=[("files", ("arch.png", b"\x89PNG\r\n\x1a\nxx", "image/png"))]).json()
    home = page(client, pid, s["id"], title="Welcome", body="Start here.")
    api = page(client, pid, s["id"], title="Payments API", parent_id=home["id"], body=f"## Webhooks\n\nRetries with backoff. ![Diagram](doc-file:{picture['id']}) See [home](doc-page:{home['id']}).\n\n<script>alert(1)</script>")
    other = site(client, pid, name="Internal")
    (secret,) = client.post(f"/api/projects/{pid}/docs/sites/{other['id']}/files", files=[("files", ("s.png", b"\x89PNG\r\n\x1a\nyy", "image/png"))]).json()
    client.cookies.clear()  # nobody signed in

    assert client.get("/docs/handbook/").status_code == 404  # a draft is not there
    assert client.get("/api/public/docs/handbook").status_code == 404
    sign_in(client, "admin", ADMIN_PASSWORD)
    client.patch(f"/api/projects/{pid}/docs/sites/{s['id']}", json={"published": True})
    client.patch(f"/api/projects/{pid}/docs/sites/{other['id']}", json={"published": True})
    client.cookies.clear()

    view = client.get("/api/public/docs/handbook").json()
    assert view["name"] == "Handbook" and view["home"] == "welcome" and view["base"] == "/docs/handbook"
    assert view["nav"] == [{"id": home["id"], "title": "Welcome", "path": "welcome", "children": [{"id": api["id"], "title": "Payments API", "path": "welcome/payments-api", "children": []}]}]
    got = client.get("/api/public/docs/handbook/page", params={"path": "welcome/payments-api"}).json()
    assert got["toc"] == [{"level": 2, "id": "webhooks", "text": "Webhooks"}] and got["breadcrumbs"] == [{"title": "Welcome", "path": "welcome"}]
    assert f'src="/api/public/docs/handbook/files/{picture["id"]}"' in got["html"] and 'href="/docs/handbook/welcome"' in got["html"]
    assert "<script>" not in got["html"] and got["prev"] == {"title": "Welcome", "path": "welcome"}
    assert client.get("/api/public/docs/handbook/page", params={"path": ""}).json()["title"] == "Welcome"

    html = client.get("/docs/handbook/welcome/payments-api")
    assert html.status_code == 200 and "<title>Payments API – Handbook</title>" in html.text and 'id="webhooks"' in html.text
    assert 'rel="canonical" href="http://testserver/docs/handbook/welcome/payments-api"' in html.text and "window.__PB_DOCS__=" in html.text
    assert client.get("/docs/handbook/nothing-here").status_code == 404
    assert "<loc>http://testserver/docs/handbook/welcome/payments-api</loc>" in client.get("/docs/handbook/sitemap.xml").text
    assert client.get(f"/api/public/docs/handbook/files/{picture['id']}").content.startswith(b"\x89PNG")
    assert client.get(f"/api/public/docs/handbook/files/{secret['id']}").status_code == 404  # another site's file

    hits = client.get("/api/public/docs/handbook/search", params={"q": "webhook retries"}).json()
    assert hits[0]["title"] == "Payments API" and "Retries" in hits[0]["snippet"]
    from plainboard.api import docs_public
    from plainboard.services.docs_public import RateLimit

    monkeypatch.setattr(docs_public, "SEARCHES", RateLimit(1))
    client.get("/api/public/docs/handbook/search", params={"q": "x"})
    assert client.get("/api/public/docs/handbook/search", params={"q": "x"}).status_code == 429
    sign_in(client, "admin", ADMIN_PASSWORD)


def test_a_site_at_its_own_domain_is_served_there_only_when_verified(client, project, monkeypatch):
    from plainboard.services import docs_domains

    pid = project["id"]
    s = site(client, pid, published=True)
    page(client, pid, s["id"], title="Welcome", body="Hello from the docs.")
    assert client.put(f"/api/projects/{pid}/docs/sites/{s['id']}/domain", json={"domain": "testserver"}).status_code == 400  # PlainBoard's own host
    setup = client.put(f"/api/projects/{pid}/docs/sites/{s['id']}/domain", json={"domain": "https://Docs.Example.com/"}).json()
    assert setup["domain"] == "docs.example.com" and setup["target"] == "testserver" and setup["verified_at"] is None
    host = {"host": "docs.example.com"}
    assert client.get("/.well-known/plainboard-docs", headers=host).text == setup["token"]
    assert client.get("/", headers=host).status_code == 404  # not verified yet

    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "docs.example.com"
        return httpx.Response(200, text=setup["token"])

    real = httpx.AsyncClient
    monkeypatch.setattr(docs_domains.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(answer), **kw))
    assert client.post(f"/api/projects/{pid}/docs/sites/{s['id']}/domain/verify").json()["verified"] is True

    client.cookies.clear()
    home = client.get("/", headers=host)
    assert home.status_code == 200 and "Hello from the docs." in home.text and 'href="http://docs.example.com/"' in home.text
    assert client.get("/api/public/docs/handbook", headers=host).json()["base"] == ""
    assert client.get("/api/projects", headers=host).status_code == 404  # nothing else of PlainBoard there
    assert client.get("/api/auth/me", headers=host).status_code == 404
    sign_in(client, "admin", ADMIN_PASSWORD)


def test_cloudflare_creates_the_cname_and_the_token_is_never_shown(client, project, monkeypatch):
    from plainboard.services import docs_domains

    pid = project["id"]
    s = site(client, pid)
    client.put(f"/api/projects/{pid}/docs/sites/{s['id']}/domain", json={"domain": "docs.shop.example.com"})
    seen = []

    def cloudflare(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, dict(request.url.params), request.headers["authorization"]))
        if request.url.path == "/client/v4/zones":
            found = [{"id": "z1", "name": "example.com"}] if request.url.params["name"] == "example.com" else []
            return httpx.Response(200, json={"success": True, "result": found})
        if request.method == "GET":
            return httpx.Response(200, json={"success": True, "result": []})
        body = json.loads(request.content)
        assert body == {"type": "CNAME", "name": "docs.shop.example.com", "content": "board.example.org", "proxied": True, "ttl": 1, "comment": "PlainBoard doc site"}
        return httpx.Response(200, json={"success": True, "result": {"id": "r1"}})

    real = httpx.AsyncClient
    monkeypatch.setattr(docs_domains.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(cloudflare), **kw))
    resp = client.post(f"/api/projects/{pid}/docs/sites/{s['id']}/domain/cloudflare", json={"token": "cf-secret-token", "target": "board.example.org"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"zone": "example.com", "record": "docs.shop.example.com", "target": "board.example.org", "created": True}
    assert [p for _, p, params, _ in seen][:2] == ["/client/v4/zones", "/client/v4/zones"] and seen[-1][0] == "POST" and seen[-1][3] == "Bearer cf-secret-token"
    shown = client.get(f"/api/projects/{pid}/docs/sites/{s['id']}").text + client.get(f"/api/projects/{pid}/docs/sites/{s['id']}/domain").text
    assert "cf-secret-token" not in shown and '"cloudflare_token_set":true' in shown


def test_agents_write_pages_by_the_doc_spec_and_attach_files(client, project):
    pid = project["id"]
    manager = manager_of(client, pid)
    dev = make_member(client, pid, "Dev")
    chat(client, manager["id"], "TOOL manage_doc_site " + json.dumps({"name": "Guide", "spec": "Audience: shop owners. Tone: friendly. Every page starts with a one-line summary."}))
    wait_for(lambda: idle(client, pid))
    assert tool_results(client, manager["id"])[-1].startswith("Created doc site guide (id ")
    chat(client, dev["id"], "TOOL write_doc_page " + json.dumps({"site": "guide", "title": "Orders", "body": "Orders arrive here.", "reason": "first page", "paths": ["app/orders"]}))
    wait_for(lambda: idle(client, pid))
    written = tool_results(client, dev["id"])[-1]
    assert written.startswith("Created doc page ") and "Doc spec of guide (follow it):\nAudience: shop owners." in written  # it had not read the spec
    page_id = int(written.split()[3])
    chat(client, dev["id"], "\n".join(["TOOL list_docs {\"site\": \"guide\"}", "TOOL read_doc_page " + json.dumps({"page": page_id}), "TOOL write_doc_page " + json.dumps({"page": page_id, "body": "Summary: orders.\n\nOrders arrive here.", "reason": "spec: summary line"})]))
    wait_for(lambda: idle(client, pid))
    listed, read, saved = tool_results(client, dev["id"])[-3:]
    assert "Doc spec of guide" in listed and f'- {page_id} /orders "Orders"' in listed and "paths: app/orders" in listed
    assert "Doc spec" not in read and "Orders arrive here." in read  # seen once in this turn
    assert "saved as a new version" in saved and "Doc spec" not in saved
    assert "manage_doc_site" not in [t.name for t in runner(client).specs[-1].bridge.tools]  # the manager's
    assert {"list_docs", "read_doc_page", "write_doc_page", "attach_doc_file"} <= {t.name for t in runner(client).specs[-1].bridge.tools}


def test_the_manager_hears_once_per_burst_which_pages_a_landing_may_have_made_outdated(client, project):
    from plainboard.models import DocPage, InboxItem, Landing, LandingStatus, utcnow
    from sqlalchemy import select

    pid = project["id"]
    client.patch(f"/api/projects/{pid}", json={"auto_wake_manager": False})
    repo = client.post(f"/api/projects/{pid}/repositories", json={"name": "api", "url": "https://github.com/acme/api.git"}).json()
    s = site(client, pid)
    pay = page(client, pid, s["id"], title="Payments", body="How payments work.", paths=["app/pay", "docs/*.md"])
    other = page(client, pid, s["id"], title="Orders", body="Orders.", paths=["app/orders"])
    fake = runner(client)
    fake.commits = [GitCommit(sha="a" * 40, message="Start", files=["README.md"])]
    watcher = client.app_ref.state.docs_watcher
    now = utcnow()
    client.portal.call(watcher.tick, now)  # starts watching from here
    assert fake.git_log_specs[-1].since is None and fake.git_log_specs[-1].limit == 1

    async def land(s, at, branch):
        s.add(Landing(project_id=pid, repository_id=repo["id"], branch=branch, title="Retry webhooks", status=LandingStatus.MERGED, merged_at=at))
        await s.commit()

    db(client, lambda s: land(s, now + timedelta(minutes=1), "be/retries"))
    fake.commits = [GitCommit(sha="c" * 40, message="Retry webhooks", files=["app/pay/webhooks.py", "app/pay/x.py"]), GitCommit(sha="b" * 40, message="Doc", files=["docs/pay.md"]), *fake.commits]
    client.portal.call(watcher.tick, now + timedelta(minutes=5))  # still landing: nothing yet
    db(client, lambda s: land(s, now + timedelta(minutes=6), "be/more"))
    client.portal.call(watcher.tick, now + timedelta(minutes=20))

    manager = agents_by_slug(client, pid)["manager"]

    async def inbox(s):
        return [i.text for i in await s.scalars(select(InboxItem).where(InboxItem.agent_id == manager["id"]))]

    texts = [t for t in db(client, inbox) if t.startswith("Docs may be outdated")]
    assert len(texts) == 1, texts
    assert "api: be/retries; be/more" in texts[0] and f'doc page {pay["id"]} "Payments" (site handbook): its linked code changed: app/pay/webhooks.py, app/pay/x.py, docs/pay.md (2 commits)' in texts[0]
    assert "Orders" not in texts[0] and fake.git_log_specs[-1].since == "a" * 40
    flagged = client.get(f"/api/projects/{pid}/docs/pages/{pay['id']}").json()
    assert flagged["flagged_at"] and flagged["drift"] == 2 and client.get(f"/api/projects/{pid}/docs/sites/{s['id']}").json()["stale_pages"] == 1
    client.portal.call(watcher.tick, now + timedelta(minutes=40))
    assert len([t for t in db(client, inbox) if t.startswith("Docs may be outdated")]) == 1  # nothing new landed

    cleared = client.post(f"/api/projects/{pid}/docs/pages/{pay['id']}/confirm").json()
    assert cleared["flagged_at"] is None and cleared["drift"] == 0

    # Daily: direct pushes count into the drift; a page whose code changed a lot is named, once a week.
    async def drift(s):
        (await s.get(DocPage, other["id"])).drift = 12
        await s.commit()

    db(client, drift)
    client.portal.call(watcher.tick, now + timedelta(days=2))
    stale = [t for t in db(client, inbox) if t.startswith("Doc pages that are probably stale")]
    assert len(stale) == 1 and f'doc page {other["id"]} "Orders"' in stale[0] and "12 commits changed its linked code" in stale[0]
    client.portal.call(watcher.tick, now + timedelta(days=3, hours=1))
    assert len([t for t in db(client, inbox) if t.startswith("Doc pages that are probably stale")]) == 1


def test_a_project_moves_with_its_doc_sites_files_and_links(client, project):
    from test_transfer import bring_in, export

    pid = project["id"]
    s = site(client, pid, spec="Be brief.", published=True)
    client.put(f"/api/projects/{pid}/docs/sites/{s['id']}/domain", json={"domain": "docs.example.com"})
    (picture,) = client.post(f"/api/projects/{pid}/docs/sites/{s['id']}/files", files=[("files", ("a.png", b"\x89PNG\r\n\x1a\nzz", "image/png"))]).json()
    top = page(client, pid, s["id"], title="Top", body="top")
    below = page(client, pid, s["id"], title="Below", parent_id=top["id"], body=f"![a](doc-file:{picture['id']}) [up](doc-page:{top['id']})")
    resp = bring_in(client, export(client, pid))
    assert resp.status_code == 201, resp.text
    new_pid = resp.json()["project"]["id"] if "project" in resp.json() else resp.json()["id"]
    (copy,) = client.get(f"/api/projects/{new_pid}/docs/sites").json()
    assert copy["slug"] == "handbook-2" and copy["spec"] == "Be brief." and copy["domain"] is None  # this installation serves the original
    pages = client.get(f"/api/projects/{new_pid}/docs/sites/{copy['id']}/pages").json()
    assert [p["path"] for p in pages] == ["top", "top/below"]
    (new_file,) = client.get(f"/api/projects/{new_pid}/docs/sites/{copy['id']}/files").json()
    body = client.get(f"/api/projects/{new_pid}/docs/pages/{pages[1]['id']}").json()["body"]
    assert body == f"![a](doc-file:{new_file['id']}) [up](doc-page:{pages[0]['id']})" and new_file["id"] != picture["id"]
    assert below["id"] != pages[1]["id"]


@pytest.mark.skipif(not POSTGRES, reason="semantic search needs PostgreSQL with pgvector")
def test_doc_pages_are_found_by_agents_people_and_the_public_search(client, project, monkeypatch):
    from test_search import FakeEmbedder, index, search

    from plainboard.services.search import embedding

    monkeypatch.setattr(embedding, "_current", {embedding.DEFAULT: FakeEmbedder()})
    pid = project["id"]
    s = site(client, pid, published=True)
    found = page(client, pid, s["id"], title="Webhooks", body="Stripe retries webhooks with exponential backoff for six hours.")
    index(client)
    result = search(client, manager_of(client, pid)["id"], pid, query="stripe webhook backoff", kinds=["doc"])
    assert f"doc page {found['id']} doc" in result and "Webhooks" in result
    palette = client.get(f"/api/projects/{pid}/search", params={"q": "stripe webhook backoff"}).json()
    assert [h.get("doc_page_id") for h in palette["hits"] if h["kind"] == "doc"] == [found["id"]], palette
    assert client.get("/api/public/docs/handbook/search", params={"q": "exponential backoff"}).json()[0]["title"] == "Webhooks"
    assert client.delete(f"/api/projects/{pid}/docs/pages/{found['id']}").status_code == 204
    assert "Nothing found." in search(client, manager_of(client, pid)["id"], pid, query="stripe webhook backoff", kinds=["doc"])
