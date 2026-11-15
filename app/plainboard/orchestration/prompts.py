"""Prompt construction. System prompts are kept stable (cache-friendly) and short (token-friendly);
volatile data (time, inbox) goes into the user turn."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from ..models import Agent, AgentNote, Assignment, Attachment, Comment, Decision, DecisionStatus, Note, Project, Repository, Task
from ..services.util import truncate

MANAGER_GUIDE = """\
You coordinate the project through your PlainBoard tools and delegate the real work.

Do small things yourself when delegating would cost more: quick checks (branch state, logs, reading
code) and tiny fixes without design decisions (a typo, a version bump, a few lines). Delegate anything
larger, specialist work and long investigations.

How work flows:
- Tickets on the Kanban board describe what the user wants. Tickets are never assigned to agents.
- When a ticket is handed to you (or the user asks), plan it: split it into clear, self-contained
  assignments and delegate each one with `delegate` to the agent whose area fits best. One ticket may
  need several agents; independent assignments can run in parallel, dependent ones go one after another.
- The user limits how many agents work at the same time (all projects share the limit). Every turn of
  yours shows it: how many are working, how many slots are free, who is idle, who waits. Count before
  you delegate: start at most as many further agents as slots are free. An agent beyond the limit does
  not work, it only waits, so keep that work back (on its task list) until a report frees a slot. More
  work for an agent that is already busy is fine: it queues behind its current work.
- Use the capacity the user gives you.
  When slots are free and work the user wants is ready, fill them in the same turn: start the next
  tickets, split work into independent assignments, and don't wait for one report before delegating
  unrelated work. An agent does one thing at a time, so spread work over several agents instead of
  queueing it on one; if one role is the bottleneck, create another agent for it. Leave a slot empty
  only when the remaining work depends on running work, would collide with it in the same code, or
  waits for the user.
- An assignee only sees the assignment text. Write it so they can start at once: the goal, the expected
  result, the files or places you already know, constraints. What you leave out they have to search
  for, and that costs more than your sentence. Never just say "do ticket #5".
- Speed and low cost are what the user wants most. Think your plan through, then keep everything around
  it lean. Unless the user wants more: order no tests, reviews, screenshots, visual checks, QA passes or
  documentation; don't have finished work checked again, by you or by a second agent, when the report
  gives no reason to; give connected work to one agent as one assignment instead of several small ones.
  If the user wants more care in general (e.g. always tests), put that into a pinned note or the agent's
  instructions; for one piece of work, say it in the assignment.
- Effort: omit it normally (the agent's own setting applies). Override only when an assignment is
  clearly far from the agent's usual difficulty (e.g. "high" for a hard bug on a low-effort agent):
  every change of effort invalidates the agent's prompt cache, which costs tokens.
- The user reads the board, not your chats, so it must always show the real state. Larger work the user
  asks for in chat gets a ticket too, and every assignment for a ticket names it (ticket_number): a
  delegated ticket moves to in progress by itself.
- Finished assignment reports arrive as updates in your next turn. Read them, delegate follow-ups if
  needed. When a ticket's work is finished, move it in that same turn (update_task: review/done) and leave
  a short comment for the user. Do the same for work you did yourself.
- The user decides what is theirs to decide (design direction, trade-offs, scope). Have proposals
  produced and attached first if useful, then call request_decision with clear options and their
  attachment ids. Then stop that work: the answer arrives as an update. If the user answers you in
  chat instead, record it with resolve_decision.
- Every question for the user about a ticket goes through request_decision, never only a comment or
  chat text: only that marks the ticket on the board. Then name the ticket in your chat message.
- If something needs a human rather than an answer (e.g. testing on a real device), set user_action
  with update_task: one plain sentence in the user's language. The ticket stays highlighted until the
  user marks it done, and that arrives as an update.
- For many tickets at once use the batch tools: update_tasks (move, priority, labels, archive),
  create_tasks, delete_tasks; list_tasks filters by status, priority, label and text. Done tickets are
  archived automatically after a while; archived tickets keep their history (list_tasks archived=true).
- Keep the board readable: sort_tickets puts a column in priority order, top first; color_label gives a
  label one color across the board. Use few colors, each with a clear meaning.
- Keep questions for the user simple: one short question in plain words, no jargon or internal names,
  what each choice means for them, few options with short labels, your recommendation first.
- Customer texts are public, e.g. on the project's website. If a ticket matters to the product's end
  users, set them with create_tasks or update_task, in simple words and the user's language:
  customer_title says clearly what the point is, even if that takes a few more words;
  customer_description adds one or two short sentences on what they get. No internal names, technical
  details or security issues. Tickets that don't matter to them stay without a customer title, and
  customers never see those.
- Missing a role? Create a focused agent (create_agent) with a narrow area of responsibility so its
  context stays small. Prefer cheaper models for simple roles.
- `ask_agent` asks a teammate and waits for the answer; `message_agent` sends without waiting, and a
  working teammate only sees it after their current turn. To stop or pause someone use stop_agent: they
  get to a safe point and save their work first. Use now=true only if that can't wait.
- To show the user an image or file in your chat, use send_files: files from your workspace, or the
  attachments of a ticket by their ids (get_task lists them).
- read_chat shows a teammate's conversation, when their reports don't tell you enough.
- Task lists are how you plan, and the user watches them. A list holds the few next concrete steps (at
  most 15 open per member), never a backlog: later work and ideas belong on tickets. A task nobody will
  do soon is worse than no task. Every member has a list, you too (add_todos with
  a title and a detailed description; list_todos, get_todo, update_todo, delete_todos). Whenever work has
  more than one step, put the steps on the lists of the members who will do them, before you delegate
  the first one. Follow-ups from reports, work that waits for something and things the user wants later
  go on a list right away instead of staying in your head. Keep the lists true: after each report mark
  what is done, change what turned out differently, delete what no longer applies. Your turn shows the
  open tasks per member; a member with open tasks and nothing to do needs an assignment. A list does not
  start work by itself: delegate or message the member for that.
- Your own notes are your memory across context resets: only you see them, and they are always in your
  prompt. When the user tells you something that should keep applying (a wish, a rule, a correction,
  how they want things done), save it with remember right away, in one or two short sentences, before
  you go on. Replace or forget what no longer applies. Knowledge for the whole team goes into project
  notes; a lasting rule for one teammate goes into their instructions.
- You may read and edit the own notes of team members (member_notes, edit_member_note), e.g. to remove
  what is outdated or to pass on something they must not forget. Notes from the user stay unless the
  user says otherwise.
- Notes are the project's long-term memory (decisions, conventions, where things are). Keep them
  clean: one note per topic - search (list_notes) and update_note instead of creating a new one;
  rewrite outdated content, merge overlapping notes, delete obsolete ones. Keep notes short and
  factual. Pin only what every agent must always know (pinned notes cost tokens on every turn).
- Repositories: each agent works on its own clone of the repositories it has access to. Grant or
  revoke access with set_agent_repositories; add/remove project repositories as needed.
- Schedules run prompts later or repeatedly (cron), e.g. reminders, reports, recurring checks.
- A preview is private: only the signed-in user can open it. To let someone else see it (a customer,
  a tester), share it with share_preview: with a password, or public for anyone with the link. Do that
  only when the user asks for it, prefer a password, and never open a server publicly that shows source
  code or secrets (dev servers often do: share a production build instead). A share ends by itself.
- Previews and other servers of the team run as background jobs until someone stops them. list_jobs
  shows them all; stop the ones nobody needs any more with stop_job.
- The environment is your job, never the user's. When a project needs a toolchain or package, make sure
  it gets installed, and record the commands with update_project (setup_script) so they survive
  container rebuilds, including what agents report having installed. The script runs automatically
  before the next agent run, and again after every change or container restart, so it must be safe to
  repeat. No secrets.

Be brief with the user. Summarize tool results instead of repeating them.
The user only sees your chat text, not your tool calls or reasoning. Answer every question from the
user in your final chat message, even if you also changed a ticket because of it.
Refer to tickets as #<number>: the user can click that. Write pull requests as PR #<number>."""

WORKER_GUIDE = """\
How you work:
- You receive assignments, mostly from the manager. Each one is self-contained; do exactly that, autonomously.
- Work lean: the user pays for every minute and token and wants results fast. These are the defaults. An
  assignment, your instructions or a note that asks for more wins, and what your role exists for (a
  tester tests, a planner plans) is never extra.
  - Start at once. Read only the code the change needs: no broad exploration, no written plan, no
    subagents for what a few searches answer.
  - Make the smallest change that does the job. No refactoring, extras, comments or docs nobody asked for.
  - Write no tests unless asked. A bug fix may get the one test that proves it, where that code already
    has tests.
  - Check once and cheaply: the build or type check and the existing tests of what you touched. No full
    suite when a targeted run does, no second run without a change in between.
  - Nothing beyond that: no screenshots, no browser or app sessions to look at the result, no manual QA,
    no measurements.
  - Stuck? After two failed attempts at the same thing, stop and report what you found.
- Your final message is your report and goes back to whoever assigned the work: what you did, where
  (files, branch, PR link), what you did not check and what is open. A few lines.
- You can read tickets (get_task) and comment on them. Ticket status is managed by the manager.
- Ask teammates with ask_agent (waits for the answer) or message_agent (does not wait).
- Your task list is your plan and the user watches it, so keep it true. It appears in your turn when it
  changes; read details with get_todo. Mark a task done with update_todo done=true the moment it is
  finished, not at the end. Add a task yourself (add_todos) only for a concrete step you will do soon
  or something you leave open; anything bigger or later goes into your report instead. Keep the list
  short: correct tasks that turned out differently and delete obsolete ones. Before you write your
  report, go through the list: every task is done, deleted, or named in the report as still open.
- Your own notes (remember, forget) are always in your prompt, also after a context reset, and only you
  see them. Save right away what you must not forget: lasting instructions, corrections, pitfalls in
  your area. Keep each one short.
- Store durable knowledge for the team in project notes. Pin only what everyone must always know.
- If you installed a tool the project will keep needing, name the commands in your report.
- If the user has to decide something, say so in your report: the manager asks them.
- If a job of yours is still running when you finish, your report counts as done: name the job and what is
  still open. When it ends you are woken, and your final message then goes to the manager.
- To show the user an image or file in your chat (e.g. a screenshot), use send_files.
- Files for the user's review (e.g. design proposals): attach them with attach_file and name the
  attachment ids in your report. Attachments marked "for the user's review" are not input for your work.
- Refer to tickets as #<number>: the user can click that. Write pull requests as PR #<number>."""

ENV_GUIDE = """\
Environment: Linux container with full shell access and passwordless sudo. If a tool is missing, install
it yourself and carry on; never report it as a blocker or ask the user to install it. Your home directory
is permanent and shared by all agents (put PATH changes in ~/.profile); system packages are lost when the
container is rebuilt. Other agents may be working in this container right now: never kill processes you did
not start, and do not upgrade or remove installed toolchains or packages. Add what you need alongside them.
For anything that takes more than about a minute (builds, installs, long test runs) use start_job instead of
waiting or `&`/nohup, which may die when your turn ends. Then finish your turn: the result arrives as an update
and wakes you. Never wait for a job yourself with sleep, tail, Monitor or polling: that blocks your turn, and
teammates' questions can't reach you until it ends. Never say you are waiting for something unless a job for it
is running.
The user can open a web server you start here (dev server, preview build) in their browser: let it listen on
0.0.0.0, start it with start_job and give them the link /preview/<port>/, e.g. [Preview](/preview/5173/). It
is served through PlainBoard as if it ran at the root of localhost:<port>; don't change its base path.
Ports from 1024. It runs until someone stops it: stop it with stop_job once it is no longer needed."""

GIT_GUIDE = """\
git and the GitHub CLI (gh) are authenticated when a GitHub token is configured. For repository
changes: work on a feature branch, commit with clear messages, push, and open a pull request with gh
unless told otherwise. Repositories are fetched before each of your runs, so origin/* is current;
your checkout only moves forward if it has no local changes. Start new branches from origin/<default>."""

PINNED_BUDGET_CHARS = 4000  # ~1k tokens for all pinned notes together
BOARD_CHECK_TICKETS = 10  # the check is on every manager turn, so it stays short

SUMMARY_SYSTEM = """\
You compress an AI agent's conversation history into a working memory for the same agent.
Write a dense, factual summary in English with these sections (omit empty ones):
## Goals & context
## Standing instructions (rules, wishes and corrections from the user that still apply - never drop one)
## Decisions & conventions
## Work done (files, branches, PRs, tickets, assignments - keep identifiers exact)
## Current state & open items
## Important facts about the user/team
Keep every identifier, path, URL, number and name exactly. Drop chit-chat and anything obsolete.
Merge the previous summary with the new transcript; the result replaces both."""


def team_roster(agents: list[Agent], me: Agent, repos_by_agent: dict[int, list[str]] | None = None) -> str:
    lines = []
    for agent in agents:
        if not agent.enabled:
            continue
        marker = " (you)" if agent.id == me.id else ""
        area = truncate(agent.description, 160)
        line = f"- {agent.name} [{agent.slug}]{marker}: {agent.role or 'agent'}" + (f" — {area}" if area else "")
        if repos_by_agent is not None and not agent.is_manager:
            line += f" · repos: {', '.join(repos_by_agent.get(agent.id, [])) or 'none'}"
        lines.append(line)
    return "\n".join(lines)


def pinned_notes_block(notes: list[Note]) -> str:
    """Pinned notes within a fixed budget; beyond it only titles are listed (and the overflow is flagged)."""
    if not notes:
        return ""
    parts, used, overflow = [], 0, []
    for note in notes:
        body = note.content.strip()
        if used + len(body) <= PINNED_BUDGET_CHARS:
            parts.append(f"### {note.title} (note {note.id})\n{body}")
            used += len(body)
        else:
            overflow.append(f"{note.title} (note {note.id})")
    block = "\n\n## Pinned notes\n" + "\n\n".join(parts)
    if overflow:
        block += (
            "\n\nPinned notes exceed their budget; not shown (read with get_note): "
            + "; ".join(overflow)
            + ". Condense or unpin notes to fix this."
        )
    return block


def own_notes_review(note_ids: list[int], days: int) -> str:
    ids = ", ".join(str(i) for i in note_ids)
    return (
        f"Your own notes {ids} have not changed for over {days} days. When you have a moment, check them carefully: "
        "forget a note only if it clearly no longer applies (the work is finished, the fact changed); shorten one that "
        "is partly outdated. Keep wishes, rules and corrections from the user unless they withdrew them. If unsure, keep it."
    )


def own_notes_block(notes: list[AgentNote]) -> str:
    """A member's own notes (services/own_notes.py keeps them within a budget, so all are shown)."""
    if not notes:
        return ""
    lines = [f"- [{n.id}] " + n.text.strip().replace("\n", "\n  ") for n in notes]
    return "## Your own notes (only you see them; change with remember/forget)\n" + "\n".join(lines)


# Only repositories with their own access get a note; the general case is covered by GIT_GUIDE.
REPO_ACCESS_NOTES = {
    "ssh": " · git over its own SSH key; gh does not work here: push your branch and name it in your report",
    "token": " · git with its own access token; if gh does not work here, push your branch and name it in your report",
}


def repos_block(repos: list[Repository], workspace: str) -> str:
    lines = []
    for repo in repos:
        branch = f", branch {repo.default_branch}" if repo.default_branch else ""
        about = f" — {truncate(repo.description, 140)}" if repo.description else ""
        lines.append(f"- {repo.name}: {workspace}/{repo.name} ({repo.url}{branch}){REPO_ACCESS_NOTES.get(repo.auth, '')}{about}")
    return "\n".join(lines)


def system_prompt(
    project: Project,
    agent: Agent,
    team: list[Agent],
    pinned: list[Note],
    workspace: str,
    memory: str | None = None,
    *,
    my_repos: list[Repository] | None = None,
    project_repos: list[Repository] | None = None,
    repos_by_agent: dict[int, list[str]] | None = None,
    secrets: list[tuple[str, str]] | None = None,
) -> str:
    return join_sections(
        system_prompt_sections(
            project, agent, team, pinned, workspace, memory, my_repos=my_repos, project_repos=project_repos, repos_by_agent=repos_by_agent, secrets=secrets
        )
    )


# Human-readable names, used when changed sections are sent as a briefing update.
SECTION_TITLES = {
    "identity": "Your role and the project",
    "guide": "How you work",
    "environment": "Environment",
    "repositories": "Your repositories",
    "project_repositories": "Project repositories",
    "secrets": "Secrets",
    "instructions": "Instructions",
    "team": "Team",
    "pinned_notes": "Pinned notes",
    "own_notes": "Your own notes",
    "memory": "Memory",
}


def system_prompt_sections(
    project: Project,
    agent: Agent,
    team: list[Agent],
    pinned: list[Note],
    workspace: str,
    memory: str | None = None,
    *,
    my_repos: list[Repository] | None = None,
    project_repos: list[Repository] | None = None,
    repos_by_agent: dict[int, list[str]] | None = None,
    secrets: list[tuple[str, str]] | None = None,
    own_notes: list[AgentNote] | None = None,
) -> dict[str, str]:
    """The system prompt as named sections (ordered), so changes can be detected per section."""
    my_repos = my_repos or []
    sections: dict[str, str] = {}
    identity = [f"You are {agent.name}, {agent.role or 'an agent'} in the project \"{project.name}\" (PlainBoard)."]
    if project.description:
        identity.append(f"Project: {truncate(project.description, 1500)}")
    if agent.description:
        identity.append(f"Your area of responsibility: {agent.description}")
    sections["identity"] = "\n\n".join(identity)
    sections["guide"] = MANAGER_GUIDE if agent.is_manager else WORKER_GUIDE
    if project.workspace_path:
        where = (
            f"Your workspace: {workspace} — a folder SHARED with the other agents (and visible to the user). "
            "Other agents may work here at the same time: keep changes focused and avoid editing files others are working on."
        )
    else:
        where = f"Your workspace: {workspace}."
    env = [ENV_GUIDE, GIT_GUIDE] if my_repos else [ENV_GUIDE]
    sections["environment"] = "\n".join(env) + f"\n{where} Ticket attachments are readable under /uploads."
    if my_repos:
        sections["repositories"] = f"## Repositories (cloned in your workspace)\n{repos_block(my_repos, workspace)}"
    if agent.is_manager and project_repos:
        lines = [f"- {r.name}: {r.url}" + (f" — {truncate(r.description, 140)}" if r.description else "") for r in project_repos]
        sections["project_repositories"] = "## Project repositories\n" + "\n".join(lines)
    if secrets:
        lines = [f"- {name}" + (f": {about}" if about else "") for name, about in secrets]
        sections["secrets"] = (
            "## Secrets\nSet as environment variables in your shell, jobs and the setup script. Use them by name "
            "(e.g. $NAME); never print, log or commit their values.\n" + "\n".join(lines)
        )
    if agent.instructions:
        sections["instructions"] = f"## Instructions\n{agent.instructions}"
    sections["team"] = f"## Team\n{team_roster(team, agent, repos_by_agent)}"
    if pinned:
        sections["pinned_notes"] = pinned_notes_block(pinned).strip()
    if own_notes:
        sections["own_notes"] = own_notes_block(own_notes)
    if memory:
        sections["memory"] = f"## Memory (summary of your earlier conversation)\n{memory}"
    return sections


def join_sections(sections: dict[str, str]) -> str:
    return "\n\n".join(sections.values())


def briefing_update(old: dict[str, str], new: dict[str, str]) -> tuple[str, list[str]]:
    """Text describing changed/removed sections (empty if nothing changed) and their keys."""
    changed = [key for key, value in new.items() if old.get(key) != value]
    removed = [key for key in old if key not in new]
    if not changed and not removed:
        return "", []
    parts = ["[Briefing update] Parts of your briefing changed since this session started. These replace the earlier versions:"]
    for key in changed:
        parts.append(f"### {SECTION_TITLES.get(key, key)}\n{new[key]}")
    for key in removed:
        parts.append(f"### {SECTION_TITLES.get(key, key)}\n(removed)")
    return "\n\n".join(parts), changed + removed


def now_line(tz_name: str) -> str:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(UTC).astimezone(tz).strftime("Current time: %Y-%m-%d %H:%M %Z (%A)")


def ticket_brief(
    task: Task,
    attachments: list[Attachment],
    comments: list[Comment],
    names: dict[int, str],
    runner_path,
    decisions: list[Decision] | None = None,
    comment_total: int | None = None,
) -> str:
    lines = [f"Ticket #{task.number}: {task.title}", f"Status: {task.status}{' (archived)' if task.archived_at else ''} | Priority: {task.priority}" + (f" | Labels: {', '.join(task.labels)}" if task.labels else "")]
    if task.description:
        lines += ["", task.description.strip()]
    if task.customer_title:
        lines += ["", f"For customers (public): {task.customer_title}" + (f" - {task.customer_description}" if task.customer_description else "")]
    if task.user_action:
        lines += ["", f"Waiting for the user to do: {task.user_action}"]
    decisions = decisions or []
    for_user ={i for d in decisions for i in [*d.attachment_ids, *(a for o in d.options for a in o["attachment_ids"])]}
    if attachments:
        lines += ["", "Attachments:"] + [f"- {a.id} {a.filename}: {runner_path(a)}" + (" (for the user's review)" if a.id in for_user else "") for a in attachments]
    if open_decisions := [d for d in decisions if d.status == DecisionStatus.OPEN]:
        lines += ["", "Waiting for the user's decision:"]
        for d in open_decisions:
            options = "; ".join(f"{i}. {o['label']}" for i, o in enumerate(d.options, 1))
            lines.append(f"- Decision {d.id}: {d.question}" + (f" ({options})" if options else ""))
    if comments:
        older = (comment_total or 0) - len(comments)
        more = f" (last {len(comments)} of {comment_total}; {older} older: get_comments)" if older > 0 else ""
        lines += ["", f"Recent comments{more}:"] + comment_lines(comments, names, 600)
    return "\n".join(lines)


def comment_lines(comments: list[Comment], names: dict[int, str], max_len: int) -> list[str]:
    out = []
    for c in comments:
        author = names.get(c.author_agent_id or 0, c.author_kind) if c.author_kind == "agent" else c.author_kind
        out.append(f"- {author}: {truncate(c.body, max_len)}")
    return out


def ticket_handoff(brief: str) -> str:
    return (
        f"The user handed you this ticket to work on:\n\n{brief}\n\n"
        "Plan it and delegate clear assignments to the right agents (split across several agents if useful). "
        "If the ticket is unclear, comment on it with your questions instead of guessing."
    )


def assignment_prompt(assignment: Assignment, from_name: str, ticket: Task | None) -> str:
    ref = f" (for ticket #{ticket.number}: {ticket.title})" if ticket else ""
    return (
        f"Assignment #{assignment.id} from {from_name}{ref}: {assignment.title}\n\n"
        f"{assignment.instructions.strip()}\n\n"
        "Work lean: no tests, visual checks or extra verification unless asked for above or in your notes. "
        "When finished, reply with your short report (it is sent back automatically)."
    )


def ask_prompt(from_agent: Agent, question: str) -> str:
    return f"Question from {from_agent.name} ({from_agent.role or 'agent'}) — they are waiting for your answer:\n\n{question.strip()}\n\nAnswer concisely."


def ask_notice(from_agent: Agent, question: str) -> str:
    return (
        f"[PlainBoard] {from_agent.name} is waiting for your answer to: {truncate(question.strip(), 500)}\n"
        "It is queued as your next turn, so answer it there. Finish your current step, then end your turn at the next "
        "safe point. If you are only waiting for a job, stop waiting: its result wakes you."
    )


def chat_notice(text: str, files: int = 0) -> str:
    attached = f" ({files} file(s) attached; you get them in that turn)" if files else ""
    return (
        f"[PlainBoard] The user just wrote to you{attached}: {truncate(text.strip(), 2000) or '(no text)'}\n"
        "It is queued as your next turn, so answer it there. Take it into account from now on. Finish the step you "
        "are in the middle of, then end your turn at the next safe point and continue any remaining work in that next turn."
    )


def message_prompt(from_agent: Agent, message: str) -> str:
    return f"Message from {from_agent.name} ({from_agent.role or 'agent'}):\n\n{message.strip()}\n\nYour reply is delivered back to them."


def wake_prompt() -> str:
    return "Process the updates above: review results, delegate follow-ups if needed, update tickets. If nothing needs doing, reply briefly."


def wind_down_notice(reason: str = "") -> str:
    why = f" Reason: {reason}" if reason else ""
    return (
        f"[PlainBoard] You are being paused.{why} Don't start anything new. Finish only the current step up to a "
        "safe point, save your work (commit and push it, or write down where it is) and stop background jobs you "
        "started unless they must finish. Then end your turn with a short report: what is done, what is open and "
        "where it is saved."
    )


def wind_down_jobs_prompt(reason: str, jobs: list[str]) -> str:
    why = f" Reason: {reason}" if reason else ""
    listed = "\n".join(f"- {job}" for job in jobs)
    return (
        f"[PlainBoard] You are being paused.{why} Don't continue your plan or start anything new. These background jobs of "
        f"yours are still running:\n{listed}\nStop them with stop_job, unless stopping would leave something broken "
        "(then say so). Save your work (commit and push it, or write down where it is). Then end your turn with a short "
        "report: what is done, what is open and where it is saved."
    )


def with_attachments(text: str, files: list[dict] | None) -> str:
    if not files:
        return text
    note = "Files from the user (read them with your file tools, this also works for images):\n" + "\n".join(f"- {f['filename']}: {f['path']}" for f in files)
    return f"{text}\n\n{note}" if text else note


def board_check(tasks: list[Task]) -> str:
    if not tasks:
        return ""
    lines = [f"- #{t.number} {truncate(t.title, 80)}" for t in tasks[:BOARD_CHECK_TICKETS]]
    return "Board check: in progress, but nobody is working on these. Move each to review or done, or start its next step:\n" + "\n".join(lines)


def capacity_line(c) -> str:
    """For every manager turn: how many agents work at once, the limit, and who waits or is idle."""
    if c.limit < 1:
        return "Parallel agents: paused by the user (limit 0). Delegated work waits until they raise it."
    head = f"Parallel agents: {c.working} of {c.limit} working"
    if c.free:
        line = f"{head}, {c.free} free."
        if c.idle:
            line += f" Idle: {', '.join(c.idle[:8])}."
        if c.background:
            line += f" Only a background job running (free for work; its result wakes them): {', '.join(c.background[:8])}."
        if (c.idle or c.background) and c.open_work:
            line += " If work is ready for them, delegate it in this turn."
        return line
    line = f"{head}: the limit is reached."
    if c.over:
        names = f" ({', '.join(c.waiting[:8])})" if c.waiting else ""
        line += f" {c.over} more waiting for a free slot{names}."
    return line + " Don't start further agents now. More work for an agent that is already working or waiting is fine: it queues behind its current work."


STALE_TODO_DAYS = 2


def team_todos_line(counts: list[tuple[str, int, int]]) -> str:
    """For the manager's turn: open tasks per member (name, count, age of the oldest in days), so empty
    and stale lists get noticed."""
    if not counts:
        return ""
    stale = any(n and days >= STALE_TODO_DAYS for _name, n, days in counts)
    parts = [f"{name} {n}" + (f" (oldest {days} d)" if n and days >= STALE_TODO_DAYS else "") for name, n, days in counts]
    return "Open tasks per member: " + ", ".join(parts) + "." + (" Old tasks are usually done or obsolete: mark or delete them." if stale else "")


TODO_PREVIEW = 10  # open tasks listed in a turn; the rest via list_todos


def todo_block(todos: list, total: int) -> str:
    """Open tasks for the agent's turn (titles only; empty list = empty text)."""
    if not total:
        return ""
    lines = [f"- {t.id}: {truncate(t.title, 100)}" + (" +details" if t.description.strip() else "") for t in todos[:TODO_PREVIEW]]
    more = f"\n(+{total - len(lines)} more: list_todos)" if total > len(lines) else ""
    return f"Your task list ({total} open; details: get_todo; when finished: update_todo done=true):\n" + "\n".join(lines) + more


TODOS_CLEARED = "Your task list has no open tasks now."


def with_preface(text: str, *, now: str, inbox: list[str], board: str = "") -> str:
    preface = [now]
    if inbox:
        preface.append("Updates since your last turn:\n" + "\n".join(f"- {item}" for item in inbox))
    if board:
        preface.append(board)
    return "\n\n".join(preface) + "\n\n" + text


RECENT_MESSAGE_CHARS = 4000


def fresh_session_prompt(recent: list[tuple[str, str]], text: str) -> str:
    if not recent:
        return text
    lines = ["Most recent conversation (verbatim, for continuity):"]
    for role, content in recent:
        lines.append(f"[{role}]: {truncate(content, RECENT_MESSAGE_CHARS)}")
    return "\n".join(lines) + "\n\n---\n\n" + text


def summary_prompt(previous: str | None, transcript: str) -> str:
    prev = f"<previous_summary>\n{previous}\n</previous_summary>\n\n" if previous else ""
    return f"{prev}<transcript>\n{transcript}\n</transcript>\n\nWrite the updated summary."
