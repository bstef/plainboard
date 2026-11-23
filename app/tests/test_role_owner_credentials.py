"""An owner by role is not the project's owner: what the project holds of its owner's credentials (and the
installation's) only the project's owner or an admin takes out or sends elsewhere (services/ownership.holds)."""

from conftest import ADMIN_PASSWORD
from test_permissions import as_friend, friend, sign_in

SECRETS = ("ghp_olga", "tok-olga", "Bearer olga", "Bearer global", "npm-olga", "PRIVATE KEY")


def olgas_project(client) -> dict:
    """Olga's project with a GitHub token, a repository with its own token, one with its own SSH key and an
    MCP server with a stored header; Gus is an owner in it by role. Signed in as Olga."""
    friend(client, may_use_shared=True)
    gus = friend(client, "gus")
    as_friend(client)
    pid = client.post("/api/projects", json={"name": "Olga's", "github_token": "ghp_olga"}).json()["id"]
    assert client.put(f"/api/projects/{pid}/members/{gus['id']}", json={"role": "owner"}).status_code == 200
    assert client.post(f"/api/projects/{pid}/secrets", json={"name": "NPM_TOKEN", "value": "npm-olga"}).status_code == 201
    token = client.post(f"/api/projects/{pid}/repositories", json={"url": "https://git.example/olga/shop.git", "auth": "token", "token": "tok-olga"}).json()
    ssh = client.post(f"/api/projects/{pid}/repositories", json={"url": "git@git.example:olga/keys.git", "auth": "ssh"}).json()
    plain = client.post(f"/api/projects/{pid}/repositories", json={"url": "https://github.com/olga/open.git"}).json()
    mcp = {"name": "docs", "transport": "http", "url": "https://mcp.example", "headers": {"Authorization": "Bearer olga"}}
    server = client.post(f"/api/projects/{pid}/mcp-servers", json=mcp).json()
    return {"pid": pid, "token": token["id"], "ssh": ssh["id"], "plain": plain["id"], "server": server["id"]}


def as_admin(client) -> None:
    sign_in(client, "admin", ADMIN_PASSWORD)


def test_only_the_projects_owner_or_an_admin_exports_its_credentials(client):
    pid = olgas_project(client)["pid"]
    as_friend(client, "gus")
    refused = client.post(f"/api/projects/{pid}/export", json={"passphrase": "mine now"})
    assert refused.status_code == 403 and "without a passphrase" in refused.json()["detail"]
    assert client.post(f"/api/projects/{pid}/export", json={}).status_code == 200  # the project without them
    for sign in (as_friend, as_admin):
        sign(client)
        assert client.post(f"/api/projects/{pid}/export", json={"passphrase": "pw"}).status_code == 200


def test_no_answer_carries_a_token_key_or_secret(client):
    ids = olgas_project(client)
    pid = ids["pid"]
    as_friend(client, "gus")
    for path in (f"/api/projects/{pid}", f"/api/projects/{pid}/repositories", f"/api/projects/{pid}/mcp-servers", f"/api/projects/{pid}/secrets", f"/api/projects/{pid}/members"):
        resp = client.get(path)
        assert resp.status_code == 200 and not [s for s in SECRETS if s in resp.text], path
    changed = client.patch(f"/api/repositories/{ids['token']}", json={"description": "Shop"})
    assert changed.status_code == 200 and "tok-olga" not in changed.text and changed.json()["has_token"]


def test_a_role_owner_gives_a_repository_with_stored_access_another_address_only_with_new_access(client):
    ids = olgas_project(client)
    as_friend(client, "gus")
    evil = "https://evil.example/olga/shop.git"
    for repo, body in ((ids["token"], {"url": evil}), (ids["ssh"], {"url": "git@evil.example:olga/keys.git"}), (ids["token"], {"url": evil, "token": " "})):
        refused = client.patch(f"/api/repositories/{repo}", json=body)
        assert refused.status_code == 403 and "Enter the token or SSH key again" in refused.json()["detail"], body
    assert client.patch(f"/api/repositories/{ids['ssh']}", json={"url": "https://git.example/olga/keys.git"}).status_code == 200  # the same place over SSH
    assert client.patch(f"/api/repositories/{ids['plain']}", json={"url": "https://evil.example/open.git"}).status_code == 200  # nothing stored goes along
    moved = client.patch(f"/api/repositories/{ids['token']}", json={"url": evil, "token": "tok-gus"})
    assert moved.status_code == 200 and moved.json()["url"] == evil
    key = client.patch(f"/api/repositories/{ids['ssh']}", json={"url": "git@evil.example:gus/keys.git", "new_ssh_key": True})
    assert key.status_code == 200 and key.json()["ssh_public_key"]
    assert client.patch(f"/api/repositories/{ids['token']}", json={"url": "https://elsewhere.example/x.git", "auth": "default"}).json()["has_token"] is False

    ids = {**ids, "repo": client.post(f"/api/projects/{ids['pid']}/repositories", json={"url": "https://git.example/olga/b.git", "name": "b", "auth": "token", "token": "t"}).json()["id"]}
    for n, sign in enumerate((as_friend, as_admin)):
        sign(client)
        resp = client.patch(f"/api/repositories/{ids['repo']}", json={"url": f"https://new{n}.example/olga/b.git"})
        assert resp.status_code == 200 and resp.json()["has_token"], resp.text


def test_a_role_owner_points_an_mcp_server_elsewhere_only_with_its_values_entered_again(client):
    ids = olgas_project(client)
    pid, sid = ids["pid"], ids["server"]
    as_admin(client)
    shared = {"name": "shared", "transport": "http", "url": "https://mcp.example", "headers": {"Authorization": "Bearer global"}, "global_scope": True}
    glob = client.post(f"/api/projects/{pid}/mcp-servers", json=shared).json()["id"]

    as_friend(client, "gus")
    for body in ({"url": "https://evil.example"}, {"url": "https://evil.example", "headers": {"Authorization": "***"}}, {"transport": "stdio", "command": "sh"}):
        refused = client.patch(f"/api/projects/{pid}/mcp-servers/{sid}", json=body)
        assert refused.status_code == 403 and "Enter them again" in refused.json()["detail"], body
    assert client.patch(f"/api/projects/{pid}/mcp-servers/{sid}", json={"enabled": False, "headers": {"Authorization": "***"}}).status_code == 200
    assert client.patch(f"/api/projects/{pid}/mcp-servers/{sid}", json={"url": "https://mcp.example"}).status_code == 200  # the same place
    assert client.patch(f"/api/projects/{pid}/mcp-servers/{sid}", json={"url": "https://other.example", "headers": {"Authorization": "Bearer gus"}}).status_code == 200
    assert client.patch(f"/api/projects/{pid}/mcp-servers/{glob}", json={"url": "https://evil.example"}).status_code == 403

    as_friend(client)  # the project's owner: its own server, not the installation's
    assert client.patch(f"/api/projects/{pid}/mcp-servers/{sid}", json={"url": "https://olga.example"}).status_code == 200
    assert client.patch(f"/api/projects/{pid}/mcp-servers/{glob}", json={"url": "https://evil.example"}).status_code == 403
    as_admin(client)
    assert client.patch(f"/api/projects/{pid}/mcp-servers/{glob}", json={"url": "https://mcp2.example"}).status_code == 200
