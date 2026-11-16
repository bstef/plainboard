# PlainBoard

[![CI](https://github.com/hthoene/plainboard/actions/workflows/ci.yml/badge.svg)](https://github.com/hthoene/plainboard/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A minimal, self-hosted workspace for a team of AI agents: a **Kanban board** and a **chat with a
project manager agent** that plans tickets, builds the team, and delegates clear assignments to
specialised agents. Agents run on Claude via the **Claude Agent SDK** (Anthropic API key, or your own
Claude login); the provider layer is ready for others such as OpenAI.

- **Kanban board**: tickets with Markdown, labels, priorities, attachments (paste or drag & drop
  images, click for a large view), and comments.
- **Your decisions**: the manager can ask you on a ticket, e.g. to choose between two design
  proposals with screenshots. Pick an option with one click, answer in your own words, or discuss it
  in chat. The ticket shows a badge until you have decided.
- **Manager chat**: the manager reads tickets, gives each one to a member as one complete assignment,
  reads the reports, and moves the tickets. It also manages agents, notes, schedules and MCP servers.
- **Built for how LLMs work, not how companies do**: an agent writes code in minutes and loses hours
  waiting, so PlainBoard cuts the waits. One member owns a change from the first line to handing it
  over. Nobody starts or awaits a check run: a member pushes its branch and hands it over
  (`merge_when_green`), and PlainBoard, without any model, merges all finished branches into one
  train, has it checked once and lands what passes. Ten changes in half an hour cost one check run,
  not ten. Time is measured and shown to the agents where they decide (how long a command took, work
  against waiting per assignment), because a model has no clock.
- **Team per project**: each agent has a role, a narrow area of responsibility, its own model,
  effort level and capabilities. Agents can ask each other (`ask_agent`) or message each other
  (`message_agent`).
- **Small contexts**: every turn re-reads its whole context, so a member starts each ticket with a
  fresh one, and any agent compresses its context once it exceeds a configurable limit, using a cheap
  model (Claude Haiku 5.5 by default). See *Token efficiency*.
- **Notes** (shared knowledge; pinned notes are in every prompt), **schedules** (one-off or cron),
  **MCP servers** (stdio/http/sse; for all or selected agents).
- **Repositories (optional)**: a project can have any number of git repositories. Each agent gets
  the repositories it has access to (all, or a selection) cloned into its workspace, one folder per
  repository. The manager can add or remove repositories and change who has access. With a
  GitHub token, agents branch, push and open PRs with `gh`. A repository can also have its own access:
  an access token (any git host, e.g. a GitLab deploy token) or an SSH key that PlainBoard creates for
  you to add as a deploy key. Without git everything works locally.
- **Notes stay tidy**: one note per topic (similar titles are rejected in favour of updating), list
  views show each note's age and size, pinned notes have a fixed token budget, and the manager is
  reminded to consolidate as the collection grows.
- **Responsive**: desktop with a sidebar, a resizable chat panel or a full-page chat; phones get a
  bottom navigation, the board's statuses stacked and foldable, and full-screen dialogs.
- **Themes**: dark (Carbon) and light (Stone), following your system until you switch. A theme is one
  block of colour tokens, so adding your own is a few lines (see `DESIGN.md`).
- **Clean chat by default**: only messages, with events as subtle one-line notes. The eye icon in the
  chat header shows agent activity (tool calls, compressions, context gauge, full event cards).

## Quick start

Requirements: Docker with Docker Compose.

```bash
docker compose up -d --build
```

Open <http://localhost:8080>, then:

1. **Settings → Claude**: paste an [Anthropic API key](https://console.anthropic.com/) and click
   **Test connection**. Alternatively you can use your own Claude login (a token from
   `claude setup-token`, or `claude login` inside the runner container) – see *Notes* below.
2. Optional, **Settings → Codex**: sign in with your ChatGPT subscription (a device code you enter
   on chatgpt.com) or paste an OpenAI API key. Then pick **Codex** as the provider of the agents that
   should run on it; the others stay on Claude.

   Both sections take several accounts (**Add account**), e.g. two Claude subscriptions. Each agent
   stays on its account while that one has room and then moves to the subscription with the most room
   left. The last 5 % of each subscription are used once all are that full; API keys only step in when
   every subscription is used up. You can pin an agent to an
   account in the team editor.
3. Optional, **Settings → GitHub**: paste a GitHub token (fine-grained or classic), or create one SSH
   key for all repositories with an SSH address and add it to your account at the git host. Or give single
   repositories their own token or SSH key (deploy key) in the project dialog; that also works for
   GitLab and other git hosts.
4. Create a project, describe what you want in the manager chat, or add tickets and move them to
   **In Progress**.

Optional configuration: copy `.env.example` to `.env` (port, password, workspace folder, credential
fallbacks).

### Working on local folders (desktop)

Set `PLAINBOARD_WORKSPACES_DIR` in `.env` to a folder on your machine, e.g.

```
PLAINBOARD_WORKSPACES_DIR=D:/Code
```

and set the project's **Local folder** to a sub folder (e.g. `my-app`). All agents of that project work
directly in `D:/Code/my-app` and you see every change immediately. Project repositories are cloned
into sub folders there (e.g. `D:/Code/my-app/backend`) if they don't exist yet. On Linux hosts the folder must be writable by UID 1000.

### Server usage

By default workspaces live in a Docker volume and every agent gets its own clones of the repositories
it may use (branches + pull requests, no conflicts between parallel agents). To expose PlainBoard,
set `PLAINBOARD_BIND=0.0.0.0` and `PLAINBOARD_PASSWORD`, ideally behind a reverse proxy with TLS. Browsers offer to install PlainBoard as an app
only over HTTPS or on `localhost`.

### Preview

Web servers that agents start (dev servers, preview builds) open through PlainBoard at
`/preview/<port>/`, e.g. <http://localhost:8080/preview/5173/>: no ports to publish, and behind the
PlainBoard password. Agents post the link in the chat. A preview is private; ask the manager to share one
with a password or publicly for a few days, e.g. for a customer. The server has to listen on `0.0.0.0`. A preview
runs on PlainBoard's own address, so only preview code you trust like your agents.

### Public API

To show progress on a website, create a key under **Project settings → Public API**. Tickets with a
customer title are then available, with their customer texts and nothing else: no internal titles,
descriptions or comments. A project can have several keys, each with a label (e.g. one per website),
so you can revoke one without breaking the others; the list shows when each was last used.

```bash
curl -H "Authorization: Bearer pb_..." https://your-plainboard/api/public/tasks
```

```json
{"project": "PaperPaw", "tasks": [{"number": 12, "title": "Faster opening of books", "description": "Large books open in about half the time.", "status": "in_progress", "updated_at": "2026-10-30T09:00:00Z"}]}
```

`status` is `backlog`, `todo`, `in_progress`, `review` or `done`. The endpoint allows calls from any
website, and responses may be cached for 60 seconds. The server must be reachable from where you call it
(see Server usage).

## How work flows

```
You ──chat──▶ Manager ──delegate──▶ Agent A ─┐
  │              ▲  ▲                         │ report (+ ticket comment)
  └─ ticket ─────┘  └──── inbox / auto-wake ──┘
```

- Tickets are **never assigned to agents directly**. Handing a ticket to the manager (button or drag to
  *In Progress*) lets it plan the work and create **assignments**: clear, self-contained work orders for
  one or more agents.
- An assignment is one piece of work with one report: it stays open while a job the member started
  for it is running, and the report is sent when it is really finished, with the time it took (work
  against waiting). Reports go to the manager's inbox and appear as ticket comments. The manager is
  woken up once per batch of reports (turn off per project with *auto-wake*), starts what comes next
  and moves the ticket to *Done* (*Review* only if you have to look at something).
- Finished branches land together. PlainBoard checks one train of branches per repository at a time,
  so check runs follow the clock instead of the number of changes. If a train fails, it is cut in
  halves until the branch that breaks the checks is found, and only that one goes back to its author.
  A repository's **check command** (e.g. `./scripts/ci.sh`) decides whether a train may land; this
  works with any git host. Without one, on GitHub, the checks of one pull request for the train decide.
- You can chat with any agent directly; the full-page **Chat** view lists all conversations.

## Token efficiency

- **Context per piece of work**: a member starts every ticket with a fresh context (system prompt,
  assignment, its notes) instead of carrying its whole history into every turn.
- **Session reuse**: within a piece of work, turns resume the Claude Code session, so prompt caching
  applies. Claude runs ask for the one-hour cache, because turns are often minutes apart.
- **Compression**: when an agent's context exceeds *Compress at (tokens)*, older messages are
  summarised with the *summary model*. The session restarts with system prompt + summary + the last
  *N* messages verbatim. Chats can grow without limit; the UI always shows the full history. The
  limit must be below the model's context window (default: 120,000); otherwise Claude Code compacts
  first, using its own threshold. A lower limit makes every turn cheaper and faster.
- **Coordinator toolset** for the manager: a short custom prompt instead of the full Claude Code
  prompt, plus board tools and a shell for small tasks that would cost more to delegate.
- **Inbox instead of extra runs**: reports, job results and messages between agents are prepended to
  an agent's next turn; wake-ups are coalesced. Waiting for CI costs no turn at all.
- **The work report** (Statistics, download) shows where turns, tokens and time went, per day,
  trigger and account: the numbers to tune a team with.
- Compact tool schemas and results, effort per agent, titles-only note listings, pinned notes only
  when needed.

## Development

```bash
# backend tests
docker compose build app runner
docker run --rm -w /src/app --entrypoint python plainboard-app -m pytest -q
docker run --rm -w /src/runner plainboard-runner python -m pytest -q

# frontend with hot reload (proxies /api to a running stack on :8080)
cd frontend && npm install && npm run dev
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the design and extension points.

## Notes

- Agents have full control of the **runner** container (shell, passwordless sudo, network). They
  cannot touch the app container. Only give them credentials you are comfortable with.
- **Claude login vs. API key:** an Anthropic API key is the officially supported way to use the Claude
  Agent SDK. PlainBoard also lets you use your own Claude login (like other open-source agent
  orchestrators do); whether that is permitted depends on Anthropic's terms for your plan – check them
  before relying on it. The same applies to a ChatGPT subscription for Codex and OpenAI's terms.
  PlainBoard is not affiliated with Anthropic or OpenAI.
- Secrets (tokens, MCP env/headers) are encrypted at rest with a key generated in the `data` volume.

## License

[MIT](LICENSE)
