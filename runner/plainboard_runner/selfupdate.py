"""An external runner updates itself to the app's version: the app sends the code it was built with
(this package and plainboard_protocol, with a sha256) through the runner's connection, the runner
checks and unpacks it next to the code it runs (``stage``), and when the app has let it run idle it
restarts into it (``restart``). The container's launcher (runner/launcher.py) switches to the new code
at the next start, on trial: it goes back to the old code when the new one does not start and connect
(``confirm``) within a few minutes, and the next hello tells the app (``status``).

Only the code changes. When the new version needs another image (its Dockerfile or launcher differ)
or other Python packages than installed here, it is refused: then only ``docker compose pull`` helps.

Layout (``PLAINBOARD_CODE_ROOT``, /opt/plainboard): ``image/`` the code of the image, ``<commit>/``
one that came by update, ``current`` → the one Python loads (plainboard.pth in the venv names it),
``next`` → the one to start next, ``trial`` while that one has not confirmed yet, ``failed`` the last
update that was rolled back."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import signal
import tarfile
import threading
import time
import tomllib
from importlib import metadata
from pathlib import Path

log = logging.getLogger("plainboard.runner.update")

ROOT = Path(os.environ.get("PLAINBOARD_CODE_ROOT", "/opt/plainboard"))
IMAGE_FILES = Path(os.environ.get("PLAINBOARD_IMAGE_FILES", "/usr/local/lib/plainboard"))  # Dockerfile, launcher.py of the image
PACKAGES = ("plainboard_runner", "plainboard_protocol")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{7,64}")
MAX_BYTES = 50 * 1024 * 1024


class Refused(Exception):
    """This runner can't take the update: only a new image helps (``docker compose pull``)."""


class Broken(Exception):
    """The update did not arrive whole (checksum, archive): another try may work."""


def _requirements_unmet(pyproject: bytes) -> list[str]:
    try:
        from packaging.requirements import Requirement
    except ImportError:  # pragma: no cover - pip brings it along
        from pip._vendor.packaging.requirements import Requirement  # type: ignore[no-redef]
    unmet = []
    for line in tomllib.loads(pyproject.decode()).get("project", {}).get("dependencies", []):
        req = Requirement(line)
        if req.name.startswith("plainboard-"):
            continue  # what comes with the update
        if req.marker is not None and not req.marker.evaluate():
            continue
        try:
            installed = metadata.version(req.name)
        except metadata.PackageNotFoundError:
            unmet.append(f"{req} (not installed)")
            continue
        if req.specifier and not req.specifier.contains(installed, prereleases=True):
            unmet.append(f"{req} (installed: {installed})")
    return unmet


def _check(files: dict[str, bytes], commit: str) -> None:
    if not os.environ.get("PLAINBOARD_LAUNCHER"):
        raise Refused("this runner was not started by the image's launcher (an image from before self-updates, or another command)")
    if files.get("COMMIT", b"").decode().strip().lower() != commit:
        raise Broken("the archive is of another version")
    for name in ("Dockerfile", "launcher.py"):
        try:
            own = (IMAGE_FILES / name).read_bytes()
        except OSError:
            raise Refused(f"the image has no {name} to compare with") from None
        if files.get(f"meta/{name}") != own:
            raise Refused(f"the new version needs a new image (its {name} changed)")
    unmet = []
    for name in ("runner.toml", "protocol.toml"):
        if (pyproject := files.get(f"meta/{name}")) is None:
            raise Broken(f"meta/{name} is missing")
        unmet += _requirements_unmet(pyproject)
    if unmet:
        raise Refused("the new version needs other Python packages: " + "; ".join(unmet))


def _unpack(data: bytes) -> dict[str, bytes]:
    files = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                name = member.name
                if name.startswith("/") or ".." in Path(name).parts or not (name in ("COMMIT",) or name.startswith(("meta/", *(f"{p}/" for p in PACKAGES)))):
                    raise Broken(f"unexpected file in the archive: {name[:100]}")
                files[name] = tar.extractfile(member).read()  # type: ignore[union-attr]
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise Broken(f"the archive can't be read: {exc}") from exc
    return files


def stage(data: bytes, sha256: str, commit: str, root: Path = ROOT) -> Path:
    """Checks the update and unpacks it next to the running code. Blocking (a thread). Raises Broken or Refused."""
    commit = commit.strip().lower()
    if not COMMIT_PATTERN.fullmatch(commit):
        raise Broken("no valid version given")
    if len(data) > MAX_BYTES:
        raise Broken("the archive is too large")
    if hashlib.sha256(data).hexdigest() != sha256.strip().lower():
        raise Broken("the checksum does not match: the update did not arrive whole")
    files = _unpack(data)
    _check(files, commit)
    target = root / commit
    fresh = root / f".{commit}.tmp"
    shutil.rmtree(fresh, ignore_errors=True)
    for name, content in files.items():
        if name.startswith("meta/"):
            continue
        path = fresh / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    if _current(root) != target.name:
        shutil.rmtree(target, ignore_errors=True)
        fresh.rename(target)
    else:
        shutil.rmtree(fresh, ignore_errors=True)
    log.info("Update to %s staged", commit[:10])
    return target


def _current(root: Path) -> str:
    try:
        return os.readlink(root / "current")
    except OSError:
        return ""


def _point(root: Path, name: str, target: str) -> None:
    tmp = root / f".{name}.link"
    tmp.unlink(missing_ok=True)
    os.symlink(target, tmp)
    os.replace(tmp, root / name)


def restart(commit: str, root: Path = ROOT, exit_after: float = 1.0) -> None:
    """The staged ``commit`` is started next: the process ends (the launcher's container with it; Docker
    starts it again). Raises Broken when nothing is staged for it (this runner restarted since)."""
    commit = commit.strip().lower()
    if not COMMIT_PATTERN.fullmatch(commit) or not all((root / commit / p).is_dir() for p in PACKAGES):
        raise Broken("nothing staged for this version")
    _point(root, "next", commit)
    log.warning("Restarting to update to %s", commit[:10])

    def stop() -> None:
        time.sleep(exit_after)  # the answer reaches the app first
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(30)
        os._exit(0)  # a shutdown that hangs: the launcher starts the new code all the same

    threading.Thread(target=stop, daemon=True).start()


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def code_commit() -> str:
    """The version of the code running, if it came by update."""
    try:
        return (Path(__file__).resolve().parent.parent / "COMMIT").read_text().strip()
    except OSError:
        return ""


def status(root: Path = ROOT) -> dict:
    """For the hello: the last update that was rolled back, if any."""
    failed = _read(root / "failed")
    return {"failed": failed} if failed.get("commit") else {}


def confirm(root: Path = ROOT) -> None:
    """The code running started and reached PlainBoard: an update on trial stays, older code goes."""
    trial = _read(root / "trial")
    if not trial:
        return
    (root / "trial").unlink(missing_ok=True)
    (root / "failed").unlink(missing_ok=True)
    keep = {"image", _current(root)}
    for path in root.iterdir():
        if path.is_dir() and not path.is_symlink() and path.name not in keep:
            shutil.rmtree(path, ignore_errors=True)
    log.info("Update to %s confirmed", str(trial.get("commit", ""))[:10])
