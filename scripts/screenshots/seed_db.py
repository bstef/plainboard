"""Runs inside the throwaway container after seed.py: the example project as it looks mid-work,
for the README screenshots. Nothing here starts an agent."""
import json, sqlite3, time
from datetime import datetime, timedelta

c = sqlite3.connect("/data/plainboard.db")
now = datetime.utcnow()
ts = lambda minutes: (now - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S.%f")
cols = lambda t: [r[1] for r in c.execute(f"pragma table_info({t})")]

def insert(table, **values):
    keys = [k for k in values if k in cols(table)]
    cur = c.execute(f"insert into {table} ({','.join(keys)}) values ({','.join('?' for _ in keys)})", [values[k] for k in keys])
    return cur.lastrowid

c.execute("update projects set label_colors=? where id=1", (json.dumps({"backend": "blue", "frontend": "teal", "content": "amber", "bug": "red", "infra": "gray"}),))
status = {5: "todo", 6: "todo", 7: "todo", 8: "in_progress", 9: "in_progress", 10: "in_progress", 11: "review", 12: "review", 13: "done", 14: "done", 15: "done"}
for tid, s in status.items():
    c.execute("update tasks set status=?, position=? where id=?", (s, tid * 1024, tid))
c.execute("update tasks set user_action=? where id=11", ("Add the search key under Project settings, Secrets, then tell the manager.",))
c.execute("update tasks set customer_title=? where id in (12, 14)", ("Order confirmation",))
insert("decisions", project_id=1, task_id=10, requested_by_agent_id=3, question="Which layout for the week view?", details="Both variants are built; screenshots are attached to the ticket.",
       options=json.dumps([{"label": "One row per day", "description": "Times as a list, compact on phones."}, {"label": "Calendar grid", "description": "Drag to set a range."}]),
       attachment_ids="[]", status="open", answer_text="", answered_by="", created_at=ts(9), updated_at=ts(9))
notes = ["Checked against the staging data, looks right. One open point: the **timeout** is still 5 s.", "Handed over, landed with the 14:10 train after one check run.", "Covered by a test now; the old one only checked the happy path."]
for tid, n in [(3, 1), (6, 2), (8, 3), (9, 6), (10, 4), (12, 2), (13, 5)]:
    for i in range(n):
        insert("comments", task_id=tid, author_kind="user" if i % 3 == 2 else "agent", author_agent_id=None if i % 3 == 2 else 2 + i % 2,
               body=notes[i % 3], content=notes[i % 3], created_at=ts(60 - i))

# Running turns: Backend on #9 for 12 minutes, Frontend on #8 for 4, the manager plans #5.
runs = {}
for agent, task, minutes in [(2, 9, 12), (3, 8, 4), (1, 5, 1)]:
    runs[agent] = insert("runs", project_id=1, agent_id=agent, trigger="ticket" if agent == 1 else "assignment", status="running", input_text="", task_id=task,
           input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_creation_tokens=0, cost_usd=0, num_turns=0, context_tokens=48000 if agent == 1 else 12000,
           created_at=ts(minutes), started_at=ts(minutes), attachment_ids="[]")

def assignment(task, agent, title, status, minutes, finished=None):
    insert("assignments", project_id=1, task_id=task, agent_id=agent, created_by_agent_id=1, title=title, instructions=title + ".", status=status,
           run_id=runs.get(agent) if status == "running" else None, created_at=ts(minutes), updated_at=ts(finished if finished is not None else minutes),
           finished_at=None if finished is None else ts(finished), result=None if finished is None else "Done, handed over.")

assignment(9, 2, "Retry failed Stripe webhooks with backoff", "running", 12)
assignment(8, 3, "Cart drawer on phones", "running", 4)
assignment(7, 3, "Show allergens on product cards", "queued", 3)
assignment(13, 2, "Login with magic link", "done", 95, 41)
assignment(14, 3, "Order confirmation page", "done", 120, 66)
assignment(15, 4, "Set up the CI check command", "done", 200, 150)

conv = c.execute("select id from conversations where agent_id=1").fetchone()
conv_id = conv[0] if conv else insert("conversations", agent_id=1, context_tokens=48000, summary="", compressions=0, total_input_tokens=0, total_output_tokens=0, total_cost_usd=0, updated_at=ts(120), session_base_tokens=0, system_prompt="", prompt_sections="{}", session_sections="{}", session_cost_usd=0)
msgs = [
    ("user", "text", "Customers should be able to pick a pickup time when they order. Each branch has its own opening hours.", {"trigger": "chat"}, 40),
    ("assistant", "text", "I split this into two tickets: #10 Opening hours editor and #5 Pickup time slots per branch. #10 goes first because the slots depend on it. Frontend starts on the editor now.", {}, 39),
    ("user", "text", "", {"trigger": "wake", "inbox": ["Backend handed over #13 Login with magic link. Landed after one check run."]}, 22),
    ("assistant", "text", "#13 Login with magic link has landed, so I moved it to Done. Backend continues with #9: the Stripe webhook retries are urgent because two orders were not marked as paid yesterday.", {}, 21),
    ("assistant", "text", "Frontend has two layouts for the week view of #10 and needs your call. Both screenshots are on the ticket.\n\n- **A** One row per day, times as a list\n- **B** Calendar grid, drag to set a range", {}, 12),
    ("user", "text", "B, but keep it to one screen on phones.", {"trigger": "chat"}, 9),
    ("assistant", "text", "Recorded on #10: calendar grid, one screen on phones. Frontend continues with that. #11 still waits for the search key from you.", {}, 9),
]
for role, kind, content, meta, minutes in msgs:
    insert("messages", conversation_id=conv_id, role=role, kind=kind, content=content, meta=json.dumps(meta), created_at=ts(minutes))

# Two subscriptions with their plan limits, so the usage rings show.
c.execute("delete from accounts")
for provider, label, key, mode in [("claude_code", "Claude Max", "demo-claude", "oauth_token"), ("codex", "ChatGPT Pro", "demo-codex", "chatgpt")]:
    insert("accounts", provider=provider, label=label, key=key, auth_mode=mode, secret_enc=None, enabled=1, created_at=ts(5000), updated_at=ts(5000))
t = int(time.time())
day = 86400
def growth(name, peak, per_day):
    days = {(now - timedelta(days=n)).date().isoformat(): per_day for n in range(0, 5)}
    return {name: {"peak": peak, "days": days}}
limits = {"accounts": {
    "demo-claude": {"windows": {"seven_day": {"name": "seven_day", "utilization": 0.46, "resets_at": t + 3 * day, "status": None, "seen_at": t - 120},
                                "seven_day_sonnet": {"name": "seven_day_sonnet", "utilization": 0.12, "resets_at": t + 3 * day, "status": None, "seen_at": t - 120}},
                    "growth": {**growth("seven_day", 0.46, 0.11), **growth("seven_day_sonnet", 0.12, 0.03)}, "updated_at": t - 120},
    "demo-codex": {"windows": {"codex_seven_day": {"name": "codex_seven_day", "utilization": 0.78, "resets_at": t + 2 * day, "status": None, "seen_at": t - 300}},
                   "growth": growth("codex_seven_day", 0.78, 0.14), "updated_at": t - 300},
}}
c.execute("insert or replace into settings (key, value, secret) values ('provider_limits', ?, 0)", (json.dumps(limits),))
c.commit()
print("seeded; conversation", conv_id, "accounts", c.execute("select id, provider, label, auth_mode from accounts").fetchall())
