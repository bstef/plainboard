# PlainBoard – notes for coding agents

Read `ARCHITECTURE.md` first. Layout: `app/` (FastAPI backend + orchestration), `runner/` (agent
execution, providers), `protocol/` (shared wire models), `frontend/` (React + TS + Tailwind v4).

## Rules
- Domain logic lives in `app/plainboard/services/`. REST endpoints (`api/`) and agent tools (`tools/`)
  are thin layers over services. Don't duplicate logic between them.
- New agent capabilities = a new `@tool` in `app/plainboard/tools/`. Keep tool descriptions and
  results short: they cost tokens on every call.
- Provider-specific code stays in `runner/plainboard_runner/providers/`. The app must stay
  provider-agnostic and talk to the runner only through `protocol` models.
- Keep system prompts stable (no timestamps) so prompt caching works. Volatile data goes into the
  user turn (`prompts.with_preface`).
- Agents are LLMs, not employees. Every turn re-reads its context, so nothing may cost a turn without
  need, and waiting is never agent work: what polls or waits is a watcher without a model
  (`job_watcher`, `landing`). Check runs are the slowest step: agents start none, PlainBoard runs one
  for all finished branches together. An agent has no clock: tool results that follow a wait say how long it
  took (`services/timing.py`). What is already ordered comes from the database (`board.in_flight`),
  never from the manager's memory. Prefer such mechanisms over more prompt text.
- Timestamps are naive UTC in the DB and serialized with a trailing `Z` (`schemas.UTCDateTime`).
- All UI strings go through i18n (`frontend/src/i18n/en.json`).
- Every API route needs a rule in `services/access.py` (`RULES`): who may read it, who may change it, and for
  a project route what names the project. Without one it is for admins only and a test fails. Endpoints don't
  check roles themselves; a list for everyone filters by the user's projects (`users.roles`).
- Schema changes need an Alembic migration (see ARCHITECTURE.md).
- SQLite is the default database, PostgreSQL an option: migrations and queries must work on both (ARCHITECTURE.md,
  "Database"). A new table, or a new column that points at another row, also needs its entry in
  `services/transfer.TABLES` (moving a project).

## Commands
```bash
docker compose up -d --build                     # run everything (http://localhost:8080)
docker run --rm -w /src/app --entrypoint python plainboard-app -m pytest -q -n auto   # -n: pytest-xdist, in parallel
docker run --rm -w /src/runner plainboard-runner python -m pytest -q
# the app's tests against PostgreSQL: the same, with -e PLAINBOARD_DATABASE=postgresql://user:password@host/name
cd frontend && npm run build                     # typecheck + production build
cd frontend && npm run dev                       # dev server, proxies /api to :8080
```
On Git Bash for Windows, prefix `docker run ... -w /src/...` with `MSYS_NO_PATHCONV=1`.
