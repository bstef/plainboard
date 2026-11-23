"""Full tool details are cut to a preview after ``tool_details_days`` (services/tool_details.py)."""

from datetime import timedelta

from conftest import agents_by_slug
from test_orchestration import conversation, make_dev
from test_scratchpad import _running_assignment


def _add(client, agent_id: int, *, kind: str, meta: dict, age_days: float, content: str = "Bash") -> int:
    from plainboard.db import sessionmaker
    from plainboard.models import Message, utcnow
    from plainboard.services import agents as agent_service

    async def create() -> int:
        async with sessionmaker()() as s:
            conv = await agent_service.conversation_for(s, agent_id)
            m = Message(conversation_id=conv.id, role="assistant", kind=kind, content=content, meta=meta, created_at=utcnow() - timedelta(days=age_days))
            s.add(m)
            await s.commit()
            return m.id

    return client.portal.call(create)


def _meta(client, message_id: int) -> dict:
    from plainboard.db import sessionmaker
    from plainboard.models import Message

    async def read():
        async with sessionmaker()() as s:
            return (await s.get(Message, message_id)).meta

    return client.portal.call(read)


def _trim(client):
    from plainboard.db import sessionmaker
    from plainboard.models import utcnow
    from plainboard.services import tool_details

    async def run():
        async with sessionmaker()() as s:
            return await tool_details.trim(s, utcnow(), batch=2)

    return client.portal.call(run)


def _set(client, value) -> None:
    from plainboard.db import sessionmaker
    from plainboard.services import settings

    async def run():
        async with sessionmaker()() as s:
            await settings.update(s, {"tool_details_days": value})

    client.portal.call(run)


def test_old_tool_details_become_a_preview(client, project):
    from plainboard.db import sessionmaker
    from plainboard.models import InboxItem, Message, utcnow
    from plainboard.orchestration.context import render_message
    from plainboard.services import settings
    from sqlalchemy import select

    pid = project["id"]
    dev = make_dev(client, pid)
    busy = make_dev(client, pid, name="Busy")
    _running_assignment(client, pid, busy, "")
    big = {"name": "Bash", "tool_use_id": "t1", "input": '{"command": "pytest -q tests/test_x.py"}' + "x" * 1400, "result": "1 failed\n" + "y" * 19_000, "is_error": True, "code": {"kind": "shell", "text": "z" * 5000}}
    # ids grow with age as in a real conversation
    text = _add(client, dev["id"], kind="text", content="a long chat text", meta={"inbox": ["y" * 5000]}, age_days=90)
    open_one = _add(client, busy["id"], kind="tool", meta=big, age_days=60)
    old2 = _add(client, dev["id"], kind="tool", meta={**big, "tool_use_id": "t2"}, age_days=40)
    old3 = _add(client, dev["id"], kind="tool", meta={**big, "tool_use_id": "t3"}, age_days=35)
    old = _add(client, dev["id"], kind="tool", meta=big, age_days=31)
    recent = _add(client, dev["id"], kind="tool", meta=big, age_days=29)

    assert client.portal.call(lambda: _get(sessionmaker, settings)) == 30  # a new installation: 30 days

    found = _trim(client)
    assert list(found) == [pid] and found[pid][0] == 3 and found[pid][1] > 3 * 20_000
    short = _meta(client, old)
    assert short["trimmed"] and short["trimmed_days"] == 30 and short["is_error"] and short["name"] == "Bash" and short["tool_use_id"] == "t1"
    assert "code" not in short and len(short["input"]) <= 300 and len(short["result"]) <= 400 and short["input"].startswith('{"command": "pytest')
    assert _meta(client, old2)["trimmed"] and _meta(client, old3)["trimmed"]
    assert _meta(client, recent) == big and _meta(client, open_one) == big  # too young; an open assignment's conversation
    assert _meta(client, text) == {"inbox": ["y" * 5000]}  # never other messages

    async def render(mid):
        async with sessionmaker()() as s:
            return render_message(await s.get(Message, mid))

    line = client.portal.call(render, old)
    assert line.startswith("[tool] Bash {\"command\": \"pytest") and "-> 1 failed" in line and line.endswith("(error)")
    shown = next(m for m in conversation(client, dev["id"])["messages"] if m["id"] == old)
    assert shown["meta"]["trimmed"] and shown["meta"]["result"].startswith("1 failed")

    assert _trim(client) == {}  # nothing twice
    _set(client, 0)  # forever
    assert _trim(client) == {}
    assert _meta(client, recent) == big

    # The watcher tells the manager once, without waking him.
    _set(client, 10)
    manager = agents_by_slug(client, pid)["manager"]

    async def watch():
        async with sessionmaker()() as s:
            values = await settings.get_all(s)
        await client.app.state.cleanup._tool_details(values, utcnow())
        async with sessionmaker()() as s:
            return [i.text for i in await s.scalars(select(InboxItem).where(InboxItem.agent_id == manager["id"]))]

    told = client.portal.call(watch)
    assert any(t.startswith("Trimmed tool details of 1 messages older than 10 days (") for t in told)
    assert _meta(client, recent)["trimmed"] and _meta(client, open_one) == big


async def _get(sessionmaker, settings):
    async with sessionmaker()() as s:
        return await settings.get(s, "tool_details_days")
