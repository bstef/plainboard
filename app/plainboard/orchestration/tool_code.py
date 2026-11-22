"""The code of a tool step, parsed for the chat: what Edit, MultiEdit, Write and Bash were given, as
fields the UI can draw (a diff, a new file, a command) without parsing the cut-off raw input."""

from __future__ import annotations

from collections.abc import Callable

FIELD_CAP = 20_000  # characters per field
STEP_CAP = 60_000  # characters per step
RESULT_CAP = 20_000  # a Bash result


def cap_text(text: str, limit: int) -> tuple[str, bool]:
    return (text, False) if len(text) <= limit else (text[:limit], True)


def code_meta(name: str, args: object, redact: Callable[[str], str]) -> dict | None:
    """`{"kind", ...fields, "truncated", "length"}` for a step that carries code, else None.
    `length` is the size of all fields before they were cut; `truncated` says whether any was."""
    if not isinstance(args, dict):
        return None
    left = [STEP_CAP]
    state = {"length": 0, "truncated": False}

    def take(value: object) -> str:
        text = redact(value) if isinstance(value, str) else ""
        state["length"] += len(text)
        text, cut = cap_text(text, max(0, min(FIELD_CAP, left[0])))
        left[0] -= len(text)
        state["truncated"] = state["truncated"] or cut
        return text

    path = args.get("file_path")
    if name == "Edit" and isinstance(path, str) and "old_string" in args:
        code: dict = {"kind": "edit", "path": take(path), "edits": [{"old": take(args["old_string"]), "new": take(args.get("new_string"))}]}
    elif name == "MultiEdit" and isinstance(path, str) and isinstance(args.get("edits"), list):
        code = {"kind": "edit", "path": take(path), "edits": [{"old": take(e.get("old_string")), "new": take(e.get("new_string"))} for e in args["edits"][:50] if isinstance(e, dict)]}
    elif name == "Write" and isinstance(path, str) and isinstance(args.get("content"), str):
        code = {"kind": "write", "path": take(path), "content": take(args["content"])}
    elif name == "Bash" and isinstance(args.get("command"), str):
        code = {"kind": "bash", "command": take(args["command"])}
    else:
        return None
    return {**code, "truncated": state["truncated"], "length": state["length"]}
