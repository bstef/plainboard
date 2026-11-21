"""Starts the runner (the command given, uvicorn) in the image, and keeps a self-update safe
(plainboard_runner/selfupdate.py). Part of the image, never of an update: only Python's standard library.

At the start, code the runner staged and asked for (``next``) becomes ``current``, on trial. If it has
not confirmed within ``TRIAL_SECONDS`` (it started and reached PlainBoard), or the runner ends before
that, or the container starts again while it is on trial, the old code is put back and ``failed`` says
why: the runner tells PlainBoard with its next hello. The launcher ends with the runner, so Docker
(restart: unless-stopped) starts the container again, with nothing of the old processes left."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("PLAINBOARD_CODE_ROOT", "/opt/plainboard"))
TRIAL_SECONDS = float(os.environ.get("PLAINBOARD_UPDATE_TRIAL_SECONDS", "300"))


def _point(name: str, target: str) -> None:
    tmp = ROOT / f".{name}.link"
    tmp.unlink(missing_ok=True)
    os.symlink(target, tmp)
    os.replace(tmp, ROOT / name)


def _trial() -> dict:
    try:
        value = json.loads((ROOT / "trial").read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _roll_back(trial: dict, reason: str) -> None:
    previous = str(trial.get("previous") or "image")
    print(f"launcher: the update to {str(trial.get('commit'))[:10]} {reason}: back to {previous}", flush=True)
    _point("current", previous)
    (ROOT / "failed").write_text(json.dumps({"commit": trial.get("commit", ""), "reason": reason, "at": time.time()}))
    (ROOT / "trial").unlink(missing_ok=True)


def prepare() -> None:
    """Before the runner starts: an update on trial that never confirmed goes, one asked for comes."""
    if not ROOT.is_dir():
        return
    if trial := _trial():
        _roll_back(trial, "did not start and reach PlainBoard (the container started again)")
        return
    nxt = ROOT / "next"
    if not nxt.is_symlink():
        return
    target = os.readlink(nxt)
    nxt.unlink()
    if all((ROOT / target / p).is_dir() for p in ("plainboard_runner", "plainboard_protocol")):
        previous = os.readlink(ROOT / "current") if (ROOT / "current").is_symlink() else "image"
        (ROOT / "trial").write_text(json.dumps({"commit": target, "previous": previous, "started": time.time()}))
        _point("current", target)
        print(f"launcher: starting the update to {target[:10]}, on trial", flush=True)


def main(argv: list[str]) -> int:
    prepare()
    child = subprocess.Popen(argv, env=dict(os.environ, PLAINBOARD_LAUNCHER="1"))
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda s, _f: child.send_signal(s))
    started = time.monotonic()
    stopping = 0.0
    while True:
        try:
            code = child.wait(timeout=5)
            break
        except subprocess.TimeoutExpired:
            pass
        if stopping and time.monotonic() - stopping > 30:
            child.kill()
        elif not stopping and (ROOT / "trial").exists() and time.monotonic() - started > TRIAL_SECONDS:
            print("launcher: the update did not confirm in time", flush=True)
            child.terminate()
            stopping = time.monotonic()
    if trial := _trial():
        _roll_back(trial, "did not confirm in time" if stopping else f"ended before it reached PlainBoard (exit code {code})")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
