"""Read a running PlainBoard from here, with a read key (Settings, "Read access").

    python scripts/remote.py /api/projects
    python scripts/remote.py /api/agents/3/conversation?limit=200
    python scripts/remote.py "/api/projects/1/work-log?days=2" -o log.jsonl.gz
    python scripts/remote.py schema
    python scripts/remote.py sql "select kind, count(*) from flow_events group by kind"
    python scripts/remote.py sql "select id, text from inbox_items order by id desc" --limit 20 --full

Where and with which key comes from the environment or from the file .remote.env next to this
repository's README (not in git):

    PLAINBOARD_REMOTE_URL=https://board.example.com
    PLAINBOARD_REMOTE_KEY=pbr_...

The key is sent to that server only and never printed. It can read and nothing else.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request


def settings() -> tuple[str, str]:
    values = dict(os.environ)
    file = pathlib.Path(__file__).resolve().parent.parent / ".remote.env"
    if file.exists():
        for line in file.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            if sep and not name.strip().startswith("#"):
                values.setdefault(name.strip(), value.strip().strip("\"'"))
    url, key = values.get("PLAINBOARD_REMOTE_URL", "").rstrip("/"), values.get("PLAINBOARD_REMOTE_KEY", "")
    if not url or not key:
        sys.exit("Set PLAINBOARD_REMOTE_URL and PLAINBOARD_REMOTE_KEY (environment or .remote.env).")
    return url, key


def main(argv: list[str]) -> None:
    if argv[:1] == ["schema"]:
        argv = ["/api/inspect/schema", *argv[1:]]
    elif argv[:1] == ["sql"] and len(argv) > 1:
        params = {"sql": argv[1], "limit": argv[argv.index("--limit") + 1] if "--limit" in argv else "200", "cell_chars": "0" if "--full" in argv else "2000"}
        argv = ["/api/inspect/query?" + urllib.parse.urlencode(params), *argv[2:]]
    if not argv or not argv[0].startswith("/api/"):
        sys.exit(__doc__)
    url, key = settings()
    out = argv[argv.index("-o") + 1] if "-o" in argv else None
    request = urllib.request.Request(url + argv[0], headers={"Authorization": f"Bearer {key}", "User-Agent": "plainboard-remote"})
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            if out:
                size = 0
                with open(out, "wb") as target:
                    while chunk := response.read(1 << 16):
                        size += target.write(chunk)
                print(f"{size} bytes -> {out}")
                return
            body = response.read()
    except urllib.error.HTTPError as exc:
        sys.exit(f"{exc.code} {exc.reason}: {exc.read().decode('utf-8', 'replace')[:300]}")
    except urllib.error.URLError as exc:
        sys.exit(f"Could not reach the server: {exc.reason}")
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        print(json.dumps(json.loads(body), indent=1, ensure_ascii=False))
    except ValueError:
        sys.stdout.buffer.write(body)


if __name__ == "__main__":
    main(sys.argv[1:])
