---
version: 1
slug: "frontend-src-features-layout-shell-tsx"
primary_target: "frontend/src/features/layout/Shell.tsx"
related_targets: ["frontend/src/features/board/Board.tsx","frontend/src/features/chat/ChatPanel.tsx"]
---

# App shell, board and chat

Scope: the whole authenticated app (shell, board, chat, team, notes, schedules, tools, stats, dialogs). Mode: Operate.
Audience and job: one person directing a team of LLM agents, on desktop and phone about equally; sees what is worked on, what waits for them, and steers with one message or click.
Constraints: self-hosted, fonts bundled; all strings through i18n; dense; dark welcome, no hacker look.

## Direction contract

THESIS: The board is a timetable. Fixed columns, ruled rows, one type size; it refuses the card-grid dashboard with a grey ground and one accent.

OWN-WORLD: Warm carbon ground (stone in light), ivory text, hairline rules, 2px corners. Vermilion means "waits for you" and nothing else; green only confirms. Sofia Sans Semi Condensed. Rank by weight, case (caps heads), reversal (plates) and rule (2px under heads). State is a mark in a fixed cell: filled, hollow, diamond, check.

STORY: The user sees who is working on what and for how long, sees what waits for them first, and opens it in one click.

FIRST VIEWPORT: Left, a 184px sidebar: brand, project field, views (current one as a reversed plate), then the "Running now" table with fixed columns (mark, agent, ticket, time). Centre, a 48px bar (title, filter, vermilion "N for you" plate, Archive, Add ticket plate) over five ruled columns with caps heads on a 2px rule. Right, the manager chat as ruled rows with name and time, composer at the bottom.

FORM: Railway timetable (third of the grounded list, chosen by the user from three built proposals); seed key e676297b.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
