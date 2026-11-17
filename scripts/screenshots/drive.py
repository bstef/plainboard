"""The README's screenshots: a throwaway instance with an example project, photographed with
headless Chrome. Nothing here starts an agent or touches a real installation.

    docker compose build app
    docker run -d --name pb-demo -p 18081:8080 -e PLAINBOARD_RUNNER_URL=http://127.0.0.1:9 plainboard-app
    python scripts/screenshots/seed_api.py http://localhost:18081          # project, agents, tickets
    docker cp scripts/screenshots/seed_db.py pb-demo:/tmp/ && docker exec pb-demo python /tmp/seed_db.py
    docker cp scripts/screenshots/_drive.html pb-demo:/app/static/         # what running turns look like
    python scripts/screenshots/drive.py http://localhost:18081 <out folder> [name ...]
    docker rm -f pb-demo

Desktop: 1440x900 at 2x. Phone: 390x844 at 2x. Both in the Classic design, dark and light. The
pictures in docs/screenshots are these, reduced to 256 colours (Pillow: quantize, no dither).
Needs Chrome at the path below and ffmpeg."""
import json
import os
import subprocess
import sys
import tempfile
import urllib.parse

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PROFILE = os.path.join(tempfile.gettempdir(), "plainboard-screenshots-profile")  # a browser profile of its own, outside the repository
BASE, OUT = sys.argv[1].rstrip("/"), sys.argv[2]
ONLY = sys.argv[3:]
os.makedirs(OUT, exist_ok=True)

TICKET = [{"t": "Opening hours editor"}]
SHOTS = []
for theme in ("dark", "light"):
    SHOTS += [
        dict(name=f"board-{theme}", kind="desktop", theme=theme, view="board", steps=[]),
        dict(name=f"chat-{theme}", kind="desktop", theme=theme, view="chat", steps=[]),
        dict(name=f"ticket-{theme}", kind="desktop", theme=theme, view="board", steps=TICKET, panel="0"),
        dict(name=f"phone-board-{theme}", kind="phone", theme=theme, view="board", steps=[]),
        dict(name=f"phone-chat-{theme}", kind="phone", theme=theme, view="chat", steps=[]),
        dict(name=f"phone-ticket-{theme}", kind="phone", theme=theme, view="board", steps=TICKET),
    ]


def shot(name, kind, theme, view, steps, panel="1"):
    q = {"theme": theme, "design": "classic", "view": view, "steps": json.dumps(steps), "panel": panel}
    size = "1440,900"
    if kind == "phone":
        q.update(w="390", h="844")
        size = "700,844"
    url = BASE + "/_drive.html?" + urllib.parse.urlencode(q)
    target = os.path.join(OUT, name + ".png")
    budget = str(9000 + 1200 * len(steps))
    subprocess.run(
        [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--lang=en-US", "--accept-lang=en-US", "--force-device-scale-factor=2", f"--window-size={size}",
         f"--virtual-time-budget={budget}", f"--user-data-dir={PROFILE}", f"--screenshot={target}", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180,
    )
    if kind == "phone":
        tmp = target + ".tmp.png"
        os.replace(target, tmp)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", tmp, "-vf", "crop=780:1688:0:0", target])
        os.remove(tmp)


for job in SHOTS:
    if ONLY and job["name"] not in ONLY:
        continue
    shot(**job)
    print("shot", job["name"], os.path.getsize(os.path.join(OUT, job["name"] + ".png")) // 1024, "KB")
