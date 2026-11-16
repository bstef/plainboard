# Architecture

```
┌──────────────────────────── docker compose ─────────────────────────────┐
│ app  (FastAPI + React SPA)                          127.0.0.1:8080       │
│  api/            REST + WebSocket (UI), /api/internal (runner callbacks) │
│  services/       domain logic, shared by REST API and agent tools        │
│  tools/          agent tool registry (single source of truth)            │
│  orchestration/  run queue, context/compression, prompts, scheduler      │
│  SQLite (WAL) + Alembic · uploads volume                                 │
│        │ POST /runs  (RunSpec → NDJSON RunEvents)      ▲ tool calls       │
│        ▼                                               │ (per-run token)  │
│ runner  (internal only; user `agent` with sudo)                          │
│  providers/      claude_code (Agent SDK) · codex (app server) · mock     │
│  tool_bridge.py  proxies tool calls to the app; local tools (files)      │
│  workspace.py    /workspaces/<project>/<agent>/<repo> or a shared folder  │
└──────────────────────────────────────────────────────────────────────────┘
protocol/  shared pydantic models (RunSpec, RunEvent, ToolDef, ...)
```

**The app is the brain, the runner only executes.** The app decides what an agent sees (system prompt,
resumed session or summary, tools, MCP servers, credentials) and sends a `RunSpec`. The runner runs it
with a provider and streams normalised `RunEvent`s back. Agents get full control of the runner
container, but the app, its database and its secrets stay out of their reach. Each run gets a random
token that is valid only while the run lasts and only for that run's project and agent.

## Core concepts

| Concept | Where | Notes |
|---|---|---|
| Ticket (`Task`) | `models.py`, `services/board.py` | Never assigned to agents. Statuses live in `TaskStatus`. |
| Public API keys | `models.PublicApiKey`, `services/api_keys.py` | Several labelled keys per project for `api/public.py`. Only the hash is stored (the key is shown once); `last_used_at` is written at most hourly. |
| Customer texts | `task.customer_title`, `task.customer_description`, `api/public.py` | Short public title and description on a ticket, mostly written by the manager. `GET /api/public/tasks` with a project key returns only tickets with a customer title, and only these texts, number, status and date. |
| Assignment | `run_manager.delegate`, `run_manager._assignment_turn_ended`, `tools/team.py` | A work order for one member, optionally linked to a ticket: one piece of work with one report. It stays open while a job started for it is running (`job.assignment_id`): the job's result starts the turn that continues it, and only the turn after which nothing of it runs any more closes it and sends the report, with its time split (`timing.ledger`). An agent has one open assignment at a time; its next one waits. `delegate` refuses a second assignment for a ticket with open work unless it is another, independent part (`parallel`). |
| Task list (`Todo`) | `services/todos.py`, `tools/todos.py`, `features/team/TodoDialog.tsx` | Planned work per team member: title and a Markdown description. The manager adds, edits, moves and deletes items; members see their own list and mark items done. Agents only see open items: titles in the turn (never the system prompt, so the cache stays valid), and within a session only when the list changed (`conversation.todos_shown`); details via `get_todo`. Done items move to the archive after 7 days and are deleted after 90 (scheduler). A list does not start a run. |
| Decision | `services/decisions.py`, `tools/board.py` | A question from the manager to the user on a ticket, with options to click and attachments for the user's review. The answer becomes a ticket comment and an inbox update that wakes the agent that asked. If the user answers in chat, the manager records it with `resolve_decision`. |
| User action | `task.user_action`, `services/board.py`, `tools/board.py` | Something only the user can do for a ticket (e.g. test on a phone), set by the manager. The ticket stays highlighted until the user marks it done, which adds a comment and wakes the manager. |
| Board marks | `project.label_colors`, `services/board.sort_column`, `tools/board.py` | The manager gives a label one color across the board (`color_label`) and orders a column (`sort_tickets`). |
| Limit forecast | `services/limits.py` | Reports only give the current utilization, so each report adds a weekly window's new peak to a per-day total (kept with the limits in `Setting`). Rate: median of the days with use × share of such days over up to 7 complete days; with fewer than 3 days, the pace of this week so far. |
| Board check | `services/board.in_flight`, `services/board.idle_in_progress`, `prompts.board_check` | The manager's state comes from the database, not from its memory. Every manager turn lists what is in flight (open assignments with their state, branches on their way to the base branch), tickets in progress that nothing is happening on (no active run, open assignment, branch being landed, open decision or user action), free capacity (`board.capacity`) and the open tasks per member. `delegate` moves a ticket to in progress. |
| Statistics | `services/stats.py`, `services/timing.py`, `task_transitions`, `features/stats/` | `stats.work_report` (the download in the view) breaks runs down per agent, trigger, ticket, day and provider account (tokens, cache reads and writes, turns, time working and waiting), counts tool calls and adds the time overview (work against waiting, slow commands by their first two words, check runs for landing against branches landed), so a change of the system can be compared before and after: numbers and tool names only. Tickets per day come from `TaskTransition`, written on every status change and kept after a ticket is deleted. |
| Review | `project.review_days`, `services/board.unmarked_in_review`, `services/board.accept_reviews`, `prompts.review_line` | Review is only for what the user has to look at: the ticket says what in `user_action` (or has an open decision), everything else goes straight to done. `update_task` says so when a ticket lands in review without that, every manager turn lists such tickets, and the scheduler moves them to done (with a system comment, no agent turn) once they lay there unchanged for the project's days. |
| Archive | `task.archived_at`, `project.auto_archive_days`, `services/board.archive_tasks` | Done tickets leave the board after the project's days (the scheduler checks) or by hand. They keep their history and links; moving or restoring one brings it back. |
| Account | `models.Account`, `services/accounts.py`, `features/settings/AccountsEditor.tsx` | Several subscriptions or API keys per provider (encrypted, write-only). `accounts.pick` chooses per turn: the agent's pinned account, else the one of its last turn while nothing better is free (the prompt cache is per account), else in this order: subscriptions below 95 % (most room first), the rest of nearly full subscriptions, API keys, used-up subscriptions. Plan limits are stored and shown per account. They arrive with the turns of an account, are read when the UI asks, and every 15 minutes by `limits_watcher` for all subscriptions, so one that is not in use is not shown as it was hours ago. On the runner each Codex account has its own Codex home (`Credentials.codex_account`) whose `sessions` link to the default home's, so threads resume under any account. Claude accounts differ only in their token. |
| Secrets | `models.Secret`, `services/secrets.py`, `Credentials.secrets` | User-defined environment variables for agents (global or per project; the project wins). Encrypted, write-only in the API. The runner sets them for the agent's shell, jobs and setup script, below git and auth variables. Agents see names and descriptions in the system prompt; values are replaced by `[secret NAME]` in stored messages and job reports. |
| Chat attachment | `services/board.py`, `api/chat.py` | A file sent with a chat message: an `Attachment` without a ticket, listed on the `Run` and in the user message's meta. The agent gets its path under `/uploads` in the turn and opens it with its file tools. |
| Preview | `services/preview.py`, `api/preview.py` | `/preview/<port>/…` is proxied to `<runner host>:<port>` (HTTP streaming and WebSockets). Private by default: only the signed-in user. The manager can share a port for a limited time (`share_preview`, `models.PreviewShare`): public, or with a password (form posts to `/api/public/preview/<port>/unlock`, which sets a signed cookie per port); the runner's own port and ports below 1024 are refused. The server sees `Host: localhost:<port>` at the root and never the session cookie. What it gets wrong because of that is repaired: `Location` headers get the prefix; a request whose `Referer` is a preview page is redirected (307) under that prefix before any other route; without a usable `Referer`, a part of a page that is neither PlainBoard's API nor one of its files goes to the last opened preview; WebSockets to the root follow the `plainboard_preview` cookie (set when a preview is opened as a page); `Set-Cookie` loses `Domain` (and `Secure` without HTTPS) and can't replace PlainBoard's cookies; a referrer policy that hides the page from its own site is relaxed; `Strict-Transport-Security` and `Clear-Site-Data` are dropped. A page that reads its own address (client-side routing) can't be repaired from outside: such a server is given the base path `/preview-base/<port>/`, a second prefix that is passed on unchanged. What a path can't give at all is an origin of its own (storage and cookies are shared with PlainBoard and other previews); that would take a subdomain per preview. |
| Browser window | runner `browser.py`, `api/browser.py`, `api/relay.py`, `features/browser/BrowserView.tsx` | A Chromium in the runner, one per project, shown in PlainBoard: there a page of an agent's server is at `localhost:<port>`, as it believes, so everything works that a path or a shared address breaks (routing, storage, cookies, sign-in redirects to localhost). The runner drives it over the DevTools protocol (`/browser/<key>`, a WebSocket): frames of the page as JPEG down, input and navigation as JSON up (the messages are listed in `browser.py`). The picture keeps pace with the viewer: the browser makes the next frame only when a viewer has confirmed the last, so a slow line gets fewer frames, never old ones; frames are small while the page moves and one sharp picture follows when it rests. The app only checks the login and relays the socket (`/api/projects/<id>/browser`); it does not read what passes. Only for the signed-in user, never for a shared preview. Only http(s) addresses; the runner's own port is refused and blocked for every page. Sign-ins are kept in the profile (`~/.plainboard/browser/<key>`). At most 3 browsers, closed 10 minutes after the last viewer left. Chromium is in the runner image unless it is built with `PLAINBOARD_BROWSER=0`. |
| Files in chat | `tools/board.py` (`send_files`), `runner/tool_bridge.py`, `api/internal.py`, `services/agents.send_files` | An agent shows files in its own chat: the runner uploads workspace files and passes on ids of existing attachments (e.g. from a ticket) in one request. The result is a message of kind `files` (`meta.attachments`), drawn like the user's attachments: images inline, large on click. It is not part of the agent's context. |
| Few turns | `run_manager.message`, `run_manager._after_run`, `run_manager._dispatch_once`, `tools/jobs.py` | Every turn re-reads the agent's context, so nothing starts one without need. A message between agents is an update in the receiver's inbox: it arrives with the next turn, several arrive together, and only an idle receiver gets a turn for it. Only an assignment's final message is a report; any other turn's final text reaches nobody (`message_agent` is explicit). A turn can wait for its job (`start_job` with `wait_minutes`, `wait_job`, up to 10 min): the result then comes back as the tool result. Started work goes before new work in the queue (a job's result, the repair of a branch for landing), and the manager's turns take none of the parallel slots, so the team never waits for it. |
| Lean work | `prompts.WORKER_GUIDE`, `prompts.MANAGER_GUIDE`, `prompts.assignment_prompt`, `claude_code.DISALLOWED_TOOLS` | The guides describe agents as what they are, LLMs, not as a company: no status updates, approvals or handovers as a routine. Members start at once, make the smallest change, decide open points themselves and name the assumption, write no tests and do no visual checks unless asked; the manager orders no tests, reviews or QA unless the user wants them and gives one member the whole path to handing its branch over. The user overrides this with a pinned note, an agent's instructions or the assignment text. The coding agent's plan mode and its own todo list are switched off. |
| Time | `services/timing.py`, `prompts.assignment_opening`, `tools/flow.py` (`time_report`), `claude_code.ToolTimer` | An agent has no clock, and its sense of time comes from people: it takes its own work for slow and a test run for quick. Here it is the other way round, so time is measured and said in plain numbers where the agent decides: a job's result and a slow tool call say how long they took, starting a command says what it took before and how often this assignment ran it, an assignment starts with the project's measured times (median work, slowest commands, the check run that lands branches), a continued assignment and every report show work against waiting, and the manager sees the team's waiting on its turn while it dominates and in `time_report`. |
| Landing | `models.Landing`, `models.Train`, `services/landings.py`, `orchestration/landing.py`, `tools/flow.py` (`merge_when_green`, `return_branch`), `runner/trains.py`, `runner/github.py` | A check run (CI, a full test suite) is the slowest step of most work, so there are as few as possible and no agent starts or awaits one. A member pushes its finished branch, hands it over with `merge_when_green` and is done. At that moment PlainBoard runs the repository's quick check (`repository.quick_check_command`: format, lint, what a minute can check) as a job in the member's own clone, on exactly the pushed commit; a failure is the tool's answer, so the member fixes it in the same turn and it never costs a check run. On a timer, without any model, the watcher merges all branches of a repository that are ready into one train (plain git in the runner's own clone, so any host works) and has it checked once: by the repository's check command (`repository.check_command`; on success the train is pushed to the base branch), or, without a command, on GitHub by the checks of one pull request for the train (gh). One train per repository at a time; what gets ready meanwhile goes on the next one, so the number of check runs follows the clock, not the number of changes. A train waits `landing_linger_seconds` for further branches before it leaves. A train that passes lands all its branches. One that fails costs no further check run to find out why: several branches fail at once, each for its own reason, and some only pass together, so there is no single branch to search for. One member (the author of most of its branches) gets the branches and the failures as one assignment (`assignment.train_id`) whose report wakes nobody: it makes them pass together and gives a branch that needs more than a small fix back to its author (`return_branch`). The repaired branches share a `landing.group_no` and are checked again as that group, before anything new joins them; meanwhile the next train leaves with what is new. A train with one branch that fails goes back to its author the same way (`assignment.landing_id`). A group whose repair changed nothing is not checked again. A branch that conflicts is left out of the train and goes back the same way. After `MAX_FAILED_ROUNDS` rounds, a repair without a push or a merge that is refused, the manager decides. Work that builds on a branch that has not landed starts from that branch. |
| Run | `run_manager.py` | One agent turn. Triggers: chat, ticket, assignment, ask, schedule, wake (`message` only in old data). A wake-up while the agent has an open assignment belongs to that assignment (`run.assignment_id`). |
| Conversation | `context.py` | One conversation per agent for the chat view, with provider session, summary and token totals. The model's context is smaller than that: see Context & compression. |
| Inbox | `orchestration/inbox.py` | Prepended to the agent's next turn: reports, job results, teammates' messages, answers of the user. Wake-ups are coalesced. |
| Job | `services/jobs.py`, `tools/jobs.py`, `orchestration/job_watcher.py`, `runner/jobs.py` | A slow shell command that runs in the runner independently of the turn that started it. The watcher polls the runner and delivers the result, with how long it took, as an inbox update with a wake-up. A job started in an assignment keeps that assignment open until it ends; a server (`start_job` with `server`) does not. Starting a command says what it cost before (`timing.job_note`), and the fourth run of the same slow command in one assignment is refused unless the agent insists (`repeat`). A runner restart loses running jobs, which is reported the same way. |
| Setup script | `project.setup_script`, `runner/workspace.py` | Maintained by the manager (no user setup): agents install what they need, the manager records the commands. The runner runs each script once per container start, one at a time, before the agent starts, so installs survive rebuilds; a failure becomes a notice in the prompt. Home (`agent-home` volume) is permanent and shared by all agents; `BASH_ENV` loads `~/.profile` in their non-interactive shells. |
| Repository | `services/repositories.py` | Many per project. Per agent: `all_repos` or an explicit selection. The runner clones them into `<workspace>/<name>` and fetches existing clones before each run; a clean checkout is fast-forwarded (not in a shared folder). A failed clone or fetch becomes a notice in the prompt instead of failing the run. Access is per repository (`repository.auth`): `default` (the general GitHub token via the gh credential helper, or none), `token` (its own HTTPS token for any host, sent as a header to that host only) or `ssh` (its own key pair, e.g. a deploy key: the app creates it and shows the public key, or takes a private key). A repository with an SSH address and no key of its own uses the general SSH key from the settings (`git_ssh_key`, set through `/api/settings/git-ssh-key`). The runner writes the access into the clone's own git config (`workspace._sync_auth`), so the agent's git commands use it too. |
| Work log | `services/flow_log.py`, `models.FlowEvent`, `services/trace.py`, `services/stats.work_report` | Two downloads in the statistics, to improve the flow itself. The **report** is numbers and tool names only (per agent, ticket, day; tool calls that came back as errors; comments per ticket by author; every train). The **full log** (`/work-log`, gzip'ed JSON lines, streamed) is everything of the period as it happened, with texts: runs with input and answer, messages with tool input and result, assignments, updates, jobs, landings, trains. Credentials never are in it (`trace.exported`). The tables keep the latest state only, so what PlainBoard does by itself is also written down as it happens: `flow_log.record(session, project_id, kind, ...)` (a train departed, failed with that output, a repair was ordered, a quick check failed, a wait was ended, an agent was paused). Nothing reads these events at run time. A new mechanism records what it decides the same way. |
| Version | `services/version.py`, `orchestration/update_watcher.py`, `runner/github.py` (`upstream`), `app/Dockerfile`, `Shell.tsx` (`UpdateNotice`) | The image knows the commit it was built from: the build reads it from the checkout's `.git` (only `HEAD` and the branch refs are in the build context, see `.dockerignore`) into `/app/commit`; without git data it is empty and nothing below happens. Every `update_interval_seconds` (6 h), without any model, the watcher has the runner ask GitHub how many commits `update_branch` of `update_repository` is ahead of that commit (the compare API; without the user's token unless the repository refuses that). Only the commit id is sent. `/api/meta` carries the answer (`version`), and the UI shows a quiet line at the foot of the sidebar and in the settings. Offline, rate limited or a commit GitHub does not know: no notice, never an error. The user turns it off with the setting `update_check`. |
| Tidy-up | `services/schedules.TIDY_*`, migrations `0021`, `0023`, `0025`, `0026`, `0027` | Every project starts with a schedule that has the manager tidy notes, schedules, tickets, task lists and the team's preview servers (background jobs; `list_jobs`/`stop_job` cover the whole team for a manager) once a day and look at the time report, carefully: only what is clearly outdated, never the user's own things. It is an ordinary schedule the user can change, pause or delete. |
| Note | `services/notes.py`, `tools/knowledge.py` | Durable knowledge, independent of chat compression. `create_note` rejects similar titles, pinned notes are capped by `prompts.PINNED_BUDGET_CHARS`, and the manager gets a tidy-up reminder every 30 notes. |
| Own notes (`AgentNote`) | `services/own_notes.py`, `tools/knowledge.py`, `features/team/OwnNotesDialog.tsx` | What one member must not forget (the user's lasting wishes, corrections). Private to the member and always in its prompt (section `own_notes`), so they survive compression and resets. Members write them with `remember`/`forget`; the user edits them in the team view. All notes of a member share `own_notes.BUDGET_CHARS`. The prompt cache stays valid: the frozen system prompt never changes, a change by the user arrives once as a briefing update, and what the member wrote itself is not sent again (`knowledge._mark_seen`). The manager reads and edits them (`member_notes`, `edit_member_note`); the member gets the change as a briefing update. A note the member or the manager wrote that is unchanged for 3 days is put up for review, and again every 7 days while it is kept (`own_notes.take_due_for_review`, run by the scheduler): an inbox line in the member's next turn, no extra turn. Notes from the user are never put up. |

### Run lifecycle (`orchestration/run_manager.py`)
1. `enqueue` creates a `Run(queued)`. The dispatcher starts it when the agent is idle and fewer than
   `max_parallel_runs` member runs are active. The limit is about members only: a manager's run
   starts as soon as the manager is free, also at limit 0 (which pauses the members), because the
   user and the team wait for it. Nested `ask_agent` runs bypass the limit, so a run that is waiting
   can never starve the run it waits for. A wait inside a turn never holds back an answer either:
   when the user writes to an agent whose turn sits in `wait_job` or `ask_agent`, that wait ends
   (`_end_waits`; the job goes on, the answer comes as an update), and a teammate's question ends a
   wait for a job. A manager can change `max_parallel_runs` itself
   (`set_parallel_agents`), up to the user's `manager_parallel_limit` (0: not at all).
2. `_prepare`: link a wake-up to the agent's open assignment, consume the inbox, store the user
   message, start a new context for a member's assignment on another ticket, build the turn
   (`ContextManager.build_turn`), issue a run token, then assemble the `RunSpec`.
3. `_stream`: persist assistant texts and tool calls, publish text deltas over the WebSocket, and track
   context size from usage events.
4. `_finish` → `_after_run`: route results (answer to the asker; for an assignment's turn either keep
   the assignment open for its job or close it: report to the requester's inbox, wake the requester;
   the report stays with the assignment, which the ticket shows, and is no comment), then `maybe_compress`. After every run `_settle` makes sure an open assignment
   that waits for nothing any more gets the turn that finishes it (also after a restart).

Queued runs start in this order: nested `ask_agent` runs, then chat messages from the user, then work
that has been started (wake-ups, repairs of branches for landing), then the rest by age. An agent's
next assignment does not start while it has an open one. A chat message for a working agent is also given to its running turn as a notice (`prompts.chat_notice`,
seen after the current tool call, nothing is interrupted) and stays queued as the next turn, so it is never
lost. Until that turn starts the user can delete the message (`withdraw_chat`). A wake-up whose updates an
earlier turn already consumed is dropped.

A restart fails running runs, but it is not a failure of the work: an interrupted assignment stays open
and its agent goes on in the same session, with a note that its last step may be half done
(`_recover_interrupted`). No manager turn and no new assignment are needed. Only an assignment that
restarts keep cutting off (`MAX_RESTART_RESUMES`), and an interrupted turn outside an assignment, are
reported: to the requester, or to the manager for its own turn.

A job outlives its run: the stop button of a run does not touch it. If a job of an assignment is still
running when its turn ends, the assignment stays open and goes on when the job ends. Cancelling an
assignment (`cancel_assignment`) stops its jobs, also when it has no turn at the moment.

`wind_down` stops an agent safely. A running turn gets an urgent notice: it arrives after the current tool
call, and a tool call still open after 60 seconds is interrupted (`claude_code.UrgentNoticeWatch`). An agent
with no turn but running jobs gets a turn for stopping them, even if it is paused. A paused (disabled) agent
keeps its assignments: the one it was working on stays open, queued ones stay queued, and all go on when
it is enabled again (`resume`). Stopping an agent without pausing it drops its queue and ends with a report.

`ask_agent` blocks on a future. A wait-for graph between agents rejects cycles, and depth and timeout
limits apply. An answer that arrives after a timeout goes to the asker's inbox instead.

### Context & compression (`orchestration/context.py`)
- Every turn re-reads its whole context, so its size is what a turn costs. A member's context belongs to
  a piece of work: an assignment for another ticket starts a new session (`new_topic`,
  `conversation.session_topic`) with the system prompt, the assignment and a line on its last
  assignments. Nothing is summarized for that; what must outlive a ticket is in the member's own notes,
  the project notes, the ticket and the repository. Further assignments for the same ticket, wake-ups,
  messages and the user's chat continue the session. The manager keeps one session.
- With a session: resume it (prompt caching, full tool history inside Claude Code). The system prompt
  is **frozen per session** (`conversation.system_prompt`) so the cached prefix stays valid. If the
  briefing changes mid-session (team, pinned notes, repositories, instructions …), only the changed
  sections are sent as a `[Briefing update]` at the start of the next turn (`prompts.briefing_update`).
  A new session (first turn, after compression or reset) gets a fresh system prompt with the current state.
- Above `agent.context_limit_tokens` (default 120k): summarise every uncovered message except the last
  `keep_recent_messages` text messages, using `agent.summary_model` via `runner /complete`. Large
  transcripts are summarised in chunks. Then drop the session and record a `summary` message.
- Fresh session: system prompt + `## Memory` (summary) + recent messages verbatim (truncated) + the new
  turn. This also works unchanged for stateless providers.
- A provider may compact its own session, e.g. mid-run near the model's context limit (`compaction`
  event). The session continues, a `summary` message without text is recorded, and the next turn
  re-sends what differs from the frozen prompt (`conversation.session_sections`), because earlier
  briefing updates may be lost. This also applies when `context_limit_tokens` is above the provider's
  own threshold: then the provider compacts and PlainBoard's compression does not start.

### Prompts (`orchestration/prompts.py`)
The system prompt is stable (cache-friendly): role, area, guide, workspace, instructions, team roster,
pinned notes, own notes, memory. Volatile data such as the current time and inbox updates goes into the user turn.
The manager uses the `board` toolset: a short custom prompt and no Claude Code preset, with shell and
file editing for small tasks it would cost more to delegate. Workers use
`full`: the Claude Code preset with the PlainBoard part appended.

## Extension points

**Add an agent tool.** Add a decorated function to a module in `app/plainboard/tools/`:

```python
class MyArgs(BaseModel):
    number: int

@tool("my_tool", "What it does (shown to the model).", MyArgs, roles=(MANAGER,))
async def my_tool(ctx: ToolContext, args: MyArgs) -> str:
    ...  # use services, return compact text
```
The schema is derived from the args model, stripped of titles, and sent with every run. The runner
needs no change. Tools that need the agent's filesystem are declared with `local_tool(...)` and
implemented in `runner/plainboard_runner/tool_bridge.py`.

**Prompt cache.** The cached prefix is tools, then system prompt, then messages: a changed tool
definition or system prompt invalidates everything after it, so both stay stable within a session. The
cache is per provider account and model. Claude runs ask for the one-hour lifetime
(`ENABLE_PROMPT_CACHING_1H`), because turns of one agent are often more than five minutes apart.

**Add a provider.**
1. Implement `Provider` in `runner/plainboard_runner/providers/<name>.py`: `info()` (models and efforts),
   `run(spec, bridge)` yielding `RunEvent`s and ending with a `ResultEvent`, and `complete(spec)`.
   Optional: `catalog()` (models the account offers), `limits()`, and `account()`/`login()`/`logout()`
   for a subscription signed in on the runner (the UI's device sign-in uses them).
2. Register it in `providers/registry.py`.
3. Add its credentials to `Credentials`, its sign-in kinds to `services/accounts.py` (`AUTH_MODES`, `fields`)
   and the accounts editor.

A provider that can't call Python tools in-process can serve the bridge as a stdio MCP server
(`plainboard_runner/mcp_stdio.py`), as Codex does. Agents choose their provider; models of the same
`tier` stand in for each other when an agent changes provider (`services/agents.equivalent_model`).

**Codex.** `providers/codex.py` drives `codex app-server` (JSON-RPC over stdio; the CLI version is
pinned in the runner's Dockerfile), one process per run. Board tools come from `mcp_stdio`, messages
for a running turn go in with `turn/steer`, and the thread id is the session id. The default account's ChatGPT
sign-in lives in `~/.codex` (agent-home volume), further accounts in `~/.plainboard/codex-accounts/<key>`;
an API key is signed in to a separate Codex home.

The UI picks up models from `/api/meta`. If the provider has no server-side sessions, return no
`session_id`: the context manager then rebuilds every turn from summary + recent messages.

**Add a ticket status.** Extend `TaskStatus` (backend), `Status` in `schemas.py`, `STATUSES` in
`frontend/src/features/board/Board.tsx`, and the `taskStatus` texts in `en.json`.

**Add a design or a theme.** The look has two levels, both chosen in the browser (`lib/theme.ts`,
`features/layout/LookDialog.tsx`) and set on `<html>`: `data-design` and `data-theme`.

- A **design** is a look: its type, corners, shadows, and how the parts are drawn. Fahrplan, the
  default, is `frontend/src/index.css`. Every other design is one stylesheet in
  `frontend/src/designs/` (`classic.css` is the example to copy), imported in `main.tsx` and listed in
  `DESIGNS` in `lib/theme.ts` and in the pre-paint script of `index.html`. It is not layered, so it
  wins over the utility classes of the components. A design has three means, in this order of
  preference: tokens (`--font-sans`, `--radius-*`, `--shadow-lg`, `--font-weight-*` and its
  colours), the semantic classes (`.plate`, `.caps`, `.mk-*`, `.rule-b`, `.rule-t`, `.sq-hue`,
  `.prose-chat`), and the named parts (`data-part`: `side`, `topbar`, `tabs`, `main`, `panel`,
  `page-head`, `board`, `column`, `column-head`, `column-body`, `ticket`, `new-ticket`, `tag`, `facts`,
  `you`, `you-plate`, `agent`, `button`, `field`, `toggle`, `badge`, `modal`, `modal-head`, `menu`,
  `msg`, `msg-head`, `note`, `composer`, `error`; some carry state such as `data-for-you`,
  `data-role`, `data-status`, `data-variant`, `data-tone`, `data-priority`, `data-hue`).
- A **theme** is one block of colour tokens of a design (`[data-theme="…"]`); each design has a dark
  and a light one.

Components never branch on the design or the theme: no `dark:` variant, no palette colour, only the
semantic colours (`bg-bg`, `text-fg-2`, `border-line`, …). A new part of the interface that a design
should be able to draw differently gets a `data-part` name. `DESIGN.md` has the rules of Fahrplan.

**Add a UI language.** Copy `frontend/src/i18n/en.json` to `<code>.json` and register it in `i18n/index.ts`.

**Database changes.** Edit `models.py`, then generate a migration:
```bash
docker run --rm -v "$PWD":/repo -w /repo/app plainboard-app sh -c \
  "PYTHONPATH=/repo/app PLAINBOARD_DATA_DIR=/tmp/db python -m plainboard.migrate revision 'describe change'"
```
Migrations run automatically on startup.

## Testing
- `app/tests`: a `FakeRunner` executes tools through the real `RunManager`. It covers ticket hand-off,
  delegation with reports and wake-ups, `ask_agent`/`message_agent`, compression, schedules, tool
  scoping and settings.
- `runner/tests`: SDK event mapping, tool bridge, workspace safety, the mock provider over HTTP.
- End-to-end without credentials: set an agent's provider to **Mock** and use the directives
  `TOOL <name> <json>` / `SAY <text>` in messages.
