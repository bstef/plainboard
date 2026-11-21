"""A runner's self-update: what arrives is checked before anything changes, and a new version that does
not come up is replaced by the old one."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from plainboard_runner import selfupdate

COMMIT = "abcdef1234567890"
# In a checkout next to the tests; in the image where the Dockerfile puts it.
LAUNCHER = next(p for p in (Path(__file__).resolve().parents[1] / "launcher.py", Path("/usr/local/lib/plainboard/launcher.py")) if p.is_file())


def _bundle(dockerfile: bytes = b"FROM x\n", runner_deps: list[str] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:

        def add(name: str, content: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))

        deps = json.dumps(runner_deps if runner_deps is not None else ["plainboard-protocol", "pytest>=1"])
        add("COMMIT", COMMIT.encode() + b"\n")
        add("plainboard_runner/__init__.py", b"")
        add("plainboard_protocol/__init__.py", b"")
        add("meta/runner.toml", f"[project]\nname = 'plainboard-runner'\ndependencies = {deps}\n".encode())
        add("meta/protocol.toml", b"[project]\nname = 'plainboard-protocol'\ndependencies = []\n")
        add("meta/Dockerfile", dockerfile)
        add("meta/launcher.py", b"# launcher\n")
    return buf.getvalue()


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    image = tmp_path / "image-files"
    image.mkdir()
    (image / "Dockerfile").write_bytes(b"FROM x\n")
    (image / "launcher.py").write_bytes(b"# launcher\n")
    monkeypatch.setattr(selfupdate, "IMAGE_FILES", image)
    monkeypatch.setenv("PLAINBOARD_LAUNCHER", "1")
    code = tmp_path / "code"
    (code / "image" / "plainboard_runner").mkdir(parents=True)
    (code / "image" / "plainboard_protocol").mkdir()
    os.symlink("image", code / "current")
    return code


def test_an_update_that_did_not_arrive_whole_changes_nothing(root):
    data = _bundle()
    with pytest.raises(selfupdate.Broken, match="checksum"):
        selfupdate.stage(data[:-10], hashlib.sha256(data).hexdigest(), COMMIT, root)
    with pytest.raises(selfupdate.Broken, match="checksum"):
        selfupdate.stage(data, "0" * 64, COMMIT, root)
    assert sorted(p.name for p in root.iterdir()) == ["current", "image"]


def test_a_version_that_needs_a_new_image_or_other_packages_is_refused(root):
    for data, why in ((_bundle(dockerfile=b"FROM y\n"), "new image"), (_bundle(runner_deps=["pytest>=999"]), "other Python packages")):
        with pytest.raises(selfupdate.Refused, match=why):
            selfupdate.stage(data, hashlib.sha256(data).hexdigest(), COMMIT, root)
    assert not (root / COMMIT).exists()


def test_a_checked_update_is_staged_and_started_by_the_launcher_on_trial_and_put_back_when_it_fails(root, monkeypatch):
    data = _bundle()
    selfupdate.stage(data, hashlib.sha256(data).hexdigest(), COMMIT, root)
    assert (root / COMMIT / "plainboard_runner" / "__init__.py").is_file() and (root / COMMIT / "COMMIT").read_text().strip() == COMMIT
    monkeypatch.setattr(selfupdate.threading, "Thread", lambda **_kw: type("T", (), {"start": lambda self: None})())
    selfupdate.restart(COMMIT, root)
    assert os.readlink(root / "next") == COMMIT

    spec = importlib.util.spec_from_file_location("launcher", LAUNCHER)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)  # type: ignore[union-attr]
    monkeypatch.setattr(launcher, "ROOT", root)
    launcher.prepare()
    assert os.readlink(root / "current") == COMMIT and not (root / "next").exists() and (root / "trial").exists()

    # The new code ends before it confirmed: the old one comes back and the next hello says so.
    assert launcher.main(["python", "-c", "raise SystemExit(3)"]) == 3
    assert os.readlink(root / "current") == "image" and not (root / "trial").exists()
    assert selfupdate.status(root)["failed"]["commit"] == COMMIT

    # Tried again and confirmed: it stays, the old failure and leftovers go.
    os.symlink(COMMIT, root / "next")
    launcher.prepare()
    selfupdate.confirm(root)
    assert os.readlink(root / "current") == COMMIT and selfupdate.status(root) == {} and not (root / "trial").exists()
