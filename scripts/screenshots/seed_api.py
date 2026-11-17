"""Seeds the throwaway demo instance with the example project of the README (no agent runs).
usage: seed_api.py <base url>"""
import http.cookiejar
import json
import sys
import urllib.request

BASE = sys.argv[1].rstrip("/") + "/api"
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with opener.open(req) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        print("ERR", method, path, e.code, e.read()[:300])
        return None


call("POST", "/auth/setup", {"username": "demo", "name": "You", "password": "demo-demo-demo"})
call("PUT", "/settings", {"values": {"setup_done": True}})
project = call("POST", "/projects", {"name": "Kiosk", "description": "Online ordering for a bakery with three branches."})
pid = project["id"]
call("PATCH", f"/projects/{pid}", {"auto_wake_manager": False})
for name, role, color, desc in [
    ("Backend", "API and database", "#3b6fd4", "Owns the order API, payments and the database schema."),
    ("Frontend", "Shop and admin UI", "#0e8f8a", "Builds the shop pages and the admin screens."),
    ("Tester", "Tests and checks", "#b7791f", "Writes tests and keeps the check command green."),
    ("Docs", "Readme and help texts", "#c2417a", ""),
]:
    call("POST", f"/projects/{pid}/agents", {"name": name, "role": role, "color": color, "description": desc})

tickets = [
    ("Export orders as CSV", ["backend"], "medium"),
    ("Dark mode for the customer page", ["frontend"], "low"),
    ("Rate-limit the public API", ["backend"], "high"),
    ("Imprint and privacy page", ["content"], "medium"),
    ("Pickup time slots per branch", ["backend", "frontend"], "high"),
    ("Email receipt after payment", ["backend"], "medium"),
    ("Show allergens on product cards", ["frontend"], "medium"),
    ("Cart drawer on phones", ["frontend"], "medium"),
    ("Stripe webhook retries", ["backend", "bug"], "urgent"),
    ("Opening hours editor", ["frontend"], "medium"),
    ("Product search with typo tolerance", ["backend"], "medium"),
    ("Image upload for products", ["frontend"], "medium"),
    ("Login with magic link", ["backend"], "medium"),
    ("Order confirmation page", ["frontend"], "medium"),
    ("Set up the CI check command", ["infra"], "medium"),
]
for title, labels, prio in tickets:
    # Created in the backlog, then placed by the database script: nothing here may start the manager.
    t = call("POST", f"/projects/{pid}/tasks", {"title": title, "status": "backlog"})
    call("PATCH", f"/tasks/{t['id']}", {"labels": labels, "priority": prio, "description": f"## Goal\n\n{title}.\n\n- Works on phones\n- Covered by a test\n\n```bash\nnpm run check\n```"})
call("POST", f"/projects/{pid}/notes", {"title": "Deploy checklist", "content": "1. Run the check command\n2. Tag the release\n3. Watch the error log for ten minutes", "pinned": True})
print("label colours:", call("GET", f"/projects/{pid}")["label_colors"])
print("seeded project", pid)
