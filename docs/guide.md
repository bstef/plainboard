# PlainBoard guide

The details behind the [README](../README.md). For the design of the code see
[ARCHITECTURE.md](../ARCHITECTURE.md).

- [Accounts and sign-in](#accounts-and-sign-in)
- [Repositories](#repositories)
- [Working on local folders](#working-on-local-folders)
- [Running on a server](#running-on-a-server)
- [Preview](#preview)
- [Browser window](#browser-window)
- [Public API](#public-api)
- [How work flows](#how-work-flows)
- [Token efficiency](#token-efficiency)
- [The team, notes and chat](#the-team-notes-and-chat)

## Accounts and sign-in

- **Settings → Claude**: paste an [Anthropic API key](https://console.anthropic.com/) and click
  **Test connection**. Alternatively use your own Claude login: a token from `claude setup-token`, or
  `claude login` inside the runner container.
- **Settings → Codex**: sign in with your ChatGPT subscription (a device code you enter on
  chatgpt.com) or paste an OpenAI API key. Then pick **Codex** as the provider of the agents that
  should run on it; the others stay on Claude.
- Both sections take several accounts (**Add account**), e.g. two Claude subscriptions. Each agent
  stays on its account while that one has room and then moves to the subscription with the most room
  left. The last 5 % of each subscription are used once all are that full; API keys only step in when
  every subscription is used up. You can pin an agent to an account in the team editor.
- The plan limits of every subscription are shown under the chat's message field, one ring per
  subscription; a click shows the windows, their resets and a forecast.

Credentials can also come from `.env` as fallbacks (see `.env.example`). The variables are prefixed
with `PLAINBOARD_` on purpose, so keys that happen to be exported in your shell are never picked up
silently.

## Repositories

A project can have any number of git repositories. Each agent gets the repositories it has access to
(all, or a selection) cloned into its workspace, one folder per repository. The manager can add or
remove repositories and change who has access. Without git everything works locally.

- **Settings → GitHub**: paste a GitHub token (fine-grained or classic), and agents branch, push and
  open pull requests with `gh`. Or create one SSH key for all repositories with an SSH address and
  add it to your account at the git host.
- A repository can also have its own access in the project dialog: an access token (any git host,
  e.g. a GitLab deploy token) or an SSH key that PlainBoard creates for you to add as a deploy key.

## Working on local folders

Set `PLAINBOARD_WORKSPACES_DIR` in `.env` to a folder on your machine, e.g.

```
PLAINBOARD_WORKSPACES_DIR=D:/Code
```

and set the project's **Local folder** to a sub folder (e.g. `my-app`). All agents of that project
work directly in `D:/Code/my-app` and you see every change immediately. Project repositories are
cloned into sub folders there (e.g. `D:/Code/my-app/backend`) if they don't exist yet. On Linux hosts
the folder must be writable by UID 1000.

## Running on a server

By default workspaces live in a Docker volume and every agent gets its own clones of the
repositories it may use (branches and pull requests, no conflicts between parallel agents). To expose
PlainBoard, set `PLAINBOARD_BIND=0.0.0.0` and `PLAINBOARD_PASSWORD`, ideally behind a reverse proxy
with TLS. Browsers offer to install PlainBoard as an app only over HTTPS or on `localhost`.

## Preview

Web servers that agents start (dev servers, preview builds) open through PlainBoard at
`/preview/<port>/`, e.g. <http://localhost:8080/preview/5173/>: no ports to publish, and behind the
PlainBoard password. Agents post the link in the chat. A preview is private; ask the manager to share
one with a password or publicly for a few days, e.g. for a customer. The server has to listen on
`0.0.0.0`. A preview runs on PlainBoard's own address, so only preview code you trust like your
agents.

The server believes it runs at the root of `localhost:<port>`, and PlainBoard repairs what goes
wrong because of the `/preview/<port>/` path: links and requests to the root, redirects, cookies,
WebSockets. One thing can't be repaired from outside: an app that routes in the browser (React
Router, Vue Router, Next.js) reads its own address and finds no page for `/preview/5173/`. Such an
app has to know its base path: the agent sets it to `/preview-base/<port>/` (e.g. `vite --base`,
`basePath` in Next.js) and gives you that link; agents are told to. Still shared between PlainBoard
and all previews is the browser's storage, and with two previews open at once a WebSocket to the root
goes to the one opened last.

## Browser window

Some apps don't work as a preview link, whatever is repaired: they sign in through a redirect to
`localhost`, or keep things in the browser's storage that then mix with PlainBoard's. For these there
is the **Browser** view: a real browser that runs next to the agents and is shown in PlainBoard.
There the page is at `localhost:<port>`, exactly as on the machine of someone developing it.

Enter an address like `localhost:5173` (or just the port), or click the window icon next to a
preview link in the chat. You see the page and use it with mouse, keyboard and touch; on phones the
keyboard button brings up the on-screen keyboard. Each project has one browser; what you sign in to
stays signed in. It is only for you, never for someone a preview is shared with.

What it is not: it is a picture of a page, so it is less sharp and a little slower than a tab of your
own browser, text can be pasted in but not copied out, and it does not download or upload files.
Where a preview link works, the link is the better way; the arrow at the right of the address opens
the same page as one.

The browser needs Chromium in the runner image (about 700 MB). To leave it out, set
`PLAINBOARD_BROWSER=0` in `.env` and rebuild.

## Public API

To show progress on a website, create a key under **Project settings → Public API**. Tickets with a
customer title are then available, with their customer texts and nothing else: no internal titles,
descriptions or comments. A project can have several keys, each with a label (e.g. one per website),
so you can revoke one without breaking the others; the list shows when each was last used.

```bash
curl -H "Authorization: Bearer pb_..." https://your-plainboard/api/public/tasks
```

```json
{"project": "Kiosk", "tasks": [{"number": 12, "title": "Faster search", "description": "Search finds products despite typos.", "status": "in_progress", "updated_at": "2026-10-30T09:00:00Z"}]}
```

`status` is `backlog`, `todo`, `in_progress`, `review` or `done`. The endpoint allows calls from any
website, and responses may be cached for 60 seconds. The server must be reachable from where you call
it (see [Running on a server](#running-on-a-server)).

## How work flows

```
You ──chat──▶ Manager ──delegate──▶ Agent A ─┐
  │              ▲  ▲                         │ report (+ ticket comment)
  └─ ticket ─────┘  └──── inbox / auto-wake ──┘
```

- Tickets are **never assigned to agents directly**. Handing a ticket to the manager (button or drag
  to *In Progress*) lets it plan the work and create **assignments**: clear, self-contained work
  orders for one or more agents.
- An assignment is one piece of work with one report: it stays open while a job the member started
  for it is running, and the report is sent when it is really finished, with the time it took (work
  against waiting). Reports go to the manager's inbox and appear as ticket comments. The manager is
  woken up once per batch of reports (turn off per project with *auto-wake*), starts what comes next
  and moves the ticket to *Done* (*Review* only if you have to look at something).
- Finished branches land together. A member pushes its branch and hands it over
  (`merge_when_green`). PlainBoard, without any model, checks one train of branches per repository at
  a time, so check runs follow the clock instead of the number of changes. If a train fails, it is
  cut in halves until the branch that breaks the checks is found, and only that one goes back to its
  author. A repository's **check command** (e.g. `./scripts/ci.sh`) decides whether a train may land;
  this works with any git host. Without one, on GitHub, the checks of one pull request for the train
  decide.
- Time is measured and shown to the agents where they decide (how long a command took, work against
  waiting per assignment), because a model has no clock.
- **Your decisions**: the manager can ask you on a ticket, e.g. to choose between two design
  proposals with screenshots. Pick an option with one click, answer in your own words, or discuss it
  in chat. The ticket is marked until you have decided.
- You can chat with any agent directly; the full-page **Chat** view lists all conversations.

## Token efficiency

- **Context per piece of work**: a member starts every ticket with a fresh context (system prompt,
  assignment, its notes) instead of carrying its whole history into every turn.
- **Session reuse**: within a piece of work, turns resume the Claude Code session, so prompt caching
  applies. Claude runs ask for the one-hour cache, because turns are often minutes apart.
- **Compression**: when an agent's context exceeds *Compress at (tokens)*, older messages are
  summarised with the *summary model* (Claude Haiku 5.5 by default). The session restarts with system
  prompt + summary + the last *N* messages verbatim. Chats can grow without limit; the UI always
  shows the full history. The limit must be below the model's context window (default: 120,000);
  otherwise Claude Code compacts first, using its own threshold. A lower limit makes every turn
  cheaper and faster.
- **Coordinator toolset** for the manager: a short custom prompt instead of the full Claude Code
  prompt, plus board tools and a shell for small tasks that would cost more to delegate.
- **Inbox instead of extra runs**: reports, job results and messages between agents are prepended to
  an agent's next turn; wake-ups are coalesced. Waiting for CI costs no turn at all.
- **The work report** (Statistics, download) shows where turns, tokens and time went, per day,
  trigger and account: the numbers to tune a team with.
- Compact tool schemas and results, effort per agent, titles-only note listings, pinned notes only
  when needed.

## The team, notes and chat

- **Team per project**: each agent has a role, a narrow area of responsibility, its own model,
  effort level and capabilities. Agents can ask each other (`ask_agent`) or message each other
  (`message_agent`).
- **Notes** are shared knowledge; pinned notes are in every prompt. One note per topic (similar
  titles are rejected in favour of updating), list views show each note's age and size, pinned notes
  have a fixed token budget, and the manager is reminded to consolidate as the collection grows.
- **Schedules** run one-off or by cron. **MCP servers** (stdio, http, sse) can be given to all or to
  selected agents.
- **Clean chat by default**: only messages, with events as one-line notes. The eye icon in the chat
  header shows agent activity (tool calls, compressions, context gauge, full event cards).
- **Responsive**: desktop with a sidebar, a resizable chat panel or a full-page chat; phones get a
  bottom navigation, the board's statuses stacked and foldable, and full-screen dialogs.
- **Designs and themes**: the palette button opens the choice. A design is the look: *Fahrplan*
  (ruled rows, one type size) or *Classic* (cards, rounded corners, an indigo accent). Each comes
  light and dark, following your system until you choose. The choice is kept per browser. A design
  is one stylesheet and a theme one block of colour tokens, so adding your own is a small change
  (see "Add a design or a theme" in [ARCHITECTURE.md](../ARCHITECTURE.md)).
