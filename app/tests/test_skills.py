import base64
import io
import json
import zipfile
from types import SimpleNamespace

import pytest
from conftest import agents_by_slug, idle, wait_for

from plainboard.services import skills
from plainboard.services.errors import ServiceError
from plainboard.tools import registry

SKILL = b"---\nname: pdf-tools\ndescription: >\n  Fills PDF forms.\n  Use it for PDFs.\n---\nSteps ...\n"


def _zip(files: dict[str, bytes], links: tuple[str, ...] = ()) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, content in files.items():
            info = zipfile.ZipInfo(name)
            if name in links:
                info.external_attr = 0o120777 << 16
            archive.writestr(info, content)
    return out.getvalue()


def test_a_skill_folder_is_checked_before_it_is_kept():
    bundle = skills.parse_upload("pdf.zip", _zip({"pdf-tools/SKILL.md": SKILL, "pdf-tools/scripts/fill.py": b"print(1)", "__MACOSX/x": b"", "pdf-tools/.DS_Store": b""}))
    assert (bundle.name, bundle.description) == ("pdf-tools", "Fills PDF forms. Use it for PDFs.")
    assert sorted(bundle.files) == ["SKILL.md", "scripts/fill.py"]  # the one top folder is the skill's
    again = skills.parse_upload("other.zip", _zip({"SKILL.md": SKILL, "scripts/fill.py": b"print(1)"}))
    assert again.hash == bundle.hash and skills.parse_upload("again.zip", bundle.pack()).hash == bundle.hash
    assert skills.parse_upload("SKILL.md", SKILL).files == {"SKILL.md": SKILL}

    for files, links, said in [
        ({"SKILL.md": SKILL, "../evil.sh": b"x"}, (), "Not a valid path"),
        ({"SKILL.md": SKILL, "/etc/passwd": b"x"}, (), "Not a valid path"),
        ({"SKILL.md": SKILL, "a\\..\\b": b"x"}, (), "Not a valid path"),
        ({"SKILL.md": SKILL, "link": b"/etc/passwd"}, ("link",), "Links are not allowed"),
        ({"README.md": b"x"}, (), "No SKILL.md"),
        ({"a/SKILL.md": SKILL, "b/x": b""}, (), "No SKILL.md"),
        ({"SKILL.md": b"no frontmatter"}, (), "frontmatter"),
        ({"SKILL.md": b"---\nname: Bad Name\ndescription: x\n---\n"}, (), "lowercase"),
        ({"SKILL.md": b"---\nname: ok\n---\n"}, (), "needs a description"),
    ]:
        with pytest.raises(ServiceError, match=said):
            skills.parse_upload("s.zip", _zip(files, links))
    with pytest.raises(ServiceError, match="at most"):
        skills.parse_upload("big.zip", _zip({"SKILL.md": SKILL, "big.bin": b"0" * (skills.MAX_FILES_BYTES + 1)}))
    with pytest.raises(ServiceError, match="zip of the skill"):
        skills.parse_upload("notes.txt", b"hello")


def test_skills_go_to_all_or_the_chosen_agents_of_a_project(client, project):
    pid = project["id"]
    team = agents_by_slug(client, pid)
    manager = team["manager"]
    other = client.post(f"/api/projects/{pid}/agents", json={"name": "Dev", "role": "Developer"}).json()

    up = lambda name, data, **form: client.post(f"/api/projects/{pid}/skills", files={"file": (name, data)}, data=form)  # noqa: E731
    first = up("pdf.zip", _zip({"pdf-tools/SKILL.md": SKILL}))
    assert first.status_code == 201, first.text
    first = first.json()
    assert (first["name"], first["all_agents"], first["file_count"]) == ("pdf-tools", True, 1)
    assert up("again.zip", _zip({"SKILL.md": SKILL})).status_code == 409  # one name per project
    assert up("bad.zip", _zip({"../x": b"", "SKILL.md": SKILL})).status_code == 400
    second = up("SKILL.md", b"---\nname: notes\ndescription: For notes\n---\n", all_agents="false", agent_ids=f"[{other['id']}, 99999]").json()
    assert second["agent_ids"] == [other["id"]]  # an agent of another project or none is dropped

    def specs(agent):
        assert client.post(f"/api/agents/{agent['id']}/messages", json={"text": "SAY hi"}).status_code == 202
        wait_for(lambda: idle(client, pid))
        return {s.name: s.hash for s in client.app_ref.state.runner.specs[-1].skills}

    assert specs(manager) == {"pdf-tools": first["content_hash"]}
    assert specs(other) == {"pdf-tools": first["content_hash"], "notes": second["content_hash"]}
    assert client.patch(f"/api/projects/{pid}/skills/{first['id']}", json={"enabled": False}).json()["enabled"] is False
    assert specs(other) == {"notes": second["content_hash"]}

    replaced = client.put(f"/api/projects/{pid}/skills/{second['id']}/files", files={"file": ("SKILL.md", b"---\nname: notes\ndescription: New\n---\nmore\n")}).json()
    assert replaced["description"] == "New" and replaced["content_hash"] != second["content_hash"] and replaced["agent_ids"] == [other["id"]]
    assert client.post(f"/api/projects/{pid}/skills/{second['id']}/reimport").status_code == 400  # uploaded, not from git
    assert client.delete(f"/api/projects/{pid}/skills/{second['id']}").status_code == 204
    assert [s["name"] for s in client.get(f"/api/projects/{pid}/skills").json()] == ["pdf-tools"]


def test_the_manager_installs_assigns_and_removes_skills(client, project):
    pid = project["id"]
    team = agents_by_slug(client, pid)
    manager = team["manager"]
    dev = client.post(f"/api/projects/{pid}/agents", json={"name": "Dev", "role": "Developer"}).json()
    runner = client.app_ref.state.runner
    runner.skills_in_git = {"https://github.com/acme/skills pdf@v1": _zip({"SKILL.md": SKILL, "fill.py": b"print(1)"})}

    def tools(*calls: tuple[str, dict]) -> list[str]:
        text = "\n".join(f"TOOL {name} {json.dumps(args)}" for name, args in calls)
        assert client.post(f"/api/agents/{manager['id']}/messages", json={"text": text}).status_code == 202
        wait_for(lambda: idle(client, pid))
        messages = client.get(f"/api/agents/{manager['id']}/conversation").json()["messages"]
        return [m["meta"]["result"] for m in messages if m["kind"] == "tool" and "result" in m["meta"]][-len(calls):]

    notes = base64.b64encode(b"---\nname: notes\ndescription: For notes\n---\n").decode()
    git, upload, bad, missing = tools(
        ("install_skill", {"url": "https://github.com/acme/skills", "subfolder": "/pdf/", "ref": "v1", "agents": ["*"]}),
        ("install_skill", {"file": "/uploads/SKILL.md", "content_b64": notes, "agents": ["dev"]}),
        ("install_skill", {"file": "x.zip", "content_b64": base64.b64encode(_zip({"../x": b"", "SKILL.md": SKILL})).decode(), "agents": ["*"]}),
        ("install_skill", {"url": "https://github.com/acme/none", "agents": ["*"]}),
    )
    assert git.startswith("Installed skill pdf-tools") and "all agents (2 files)" in git
    assert upload.startswith("Installed skill notes") and "for dev (1 files)" in upload
    assert "Not a valid path" in bad and "git clone failed" in missing
    rows = {s["name"]: s for s in client.get(f"/api/projects/{pid}/skills").json()}
    assert set(rows) == {"pdf-tools", "notes"}
    assert (rows["pdf-tools"]["source"], rows["pdf-tools"]["source_path"], rows["pdf-tools"]["source_ref"]) == ("git", "pdf", "v1")
    assert (rows["notes"]["all_agents"], rows["notes"]["agent_ids"]) == (False, [dev["id"]])

    off, moved, listed, removed, gone = tools(
        ("update_skill", {"skill": "pdf-tools", "enabled": False}),
        ("update_skill", {"skill": str(rows["notes"]["id"]), "agents": ["manager", "dev"]}),
        ("list_skills", {}),
        ("remove_skill", {"skill": "pdf-tools"}),
        ("remove_skill", {"skill": "pdf-tools"}),
    )
    assert off == "Skill pdf-tools: off, all agents." and moved == "Skill notes: on, manager, dev."
    assert "pdf-tools (off, all agents; git https://github.com/acme/skills pdf @v1)" in listed and "notes (on, manager, dev; upload)" in listed
    assert removed == "Removed skill pdf-tools." and "No skill 'pdf-tools'" in gone
    assert [s["name"] for s in client.get(f"/api/projects/{pid}/skills").json()] == ["notes"]

    assert "install_skill" not in {t.name for t in registry.tools_for(SimpleNamespace(is_manager=False))}  # the manager's alone
