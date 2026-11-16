# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

One person (a developer or technical lead) who directs a small team of LLM agents on their own
projects. They use PlainBoard on the desktop and on the phone about equally: at the desk it stays
open next to the editor while agents work, on the phone they check in, decide what the manager asks,
and write to the manager.

## Product Purpose

A minimal, self-hosted workspace for a team of AI agents: a Kanban board and a chat with a project
manager agent that plans tickets, builds the team and delegates complete assignments to specialised
agents. Success: the user sees at a glance what is being worked on, what is waiting for them, and
can steer with one message or one click.

## Positioning

Built for how LLMs work, not how companies do. Waiting is taken out of the agents' hands: members
hand over finished branches, and PlainBoard (without a model) merges them into one train, has it
checked once and lands what passes. Time is measured and shown, because a model has no clock.

## Operating Context

- Views: board, chat, team, notes, schedules, tools (MCP servers), stats; settings and project
  dialogs; ticket dialog with Markdown, comments, attachments and decisions.
- Desktop: board with a resizable manager chat panel, or a full-page chat. Phone: bottom navigation,
  swipeable board columns, full-screen dialogs. Installable as a PWA.
- Tickets move through Backlog, To Do, In Progress, Review, Done. A ticket can wait for the user
  (a decision or a user action); that state must stand out.
- Chat is clean by default (messages, events as one-line notes); a details toggle shows tool calls,
  compressions and the context gauge.

## Capabilities and Constraints

- React + TypeScript + Tailwind v4, lucide icons. All UI strings go through i18n.
- Self-hosted via Docker, also used without internet access to third parties: fonts and assets are
  bundled, never loaded from a CDN.
- Agents have a user-chosen colour; labels have a fixed set of ten colours.
- Theming: light and dark today (system preference). The redesign should support themes as a
  first-class feature.

## Brand Commitments

- Name: PlainBoard. Plain is the promise: minimal, tidy, practical.
- A dark "midnight" look is welcome, but no exaggerated hacker or terminal look.
- Not sterile enterprise grey, not playful or colourful, not low-density. Modern, minimal, well
  thought out; must not look AI-generated.

## Product Principles

1. Nothing costs a turn or a glance without need: show state, do not make the user hunt for it.
2. What waits for the user comes first.
3. Density with order: many tickets and much chat visible, with consistent spacing and alignment.
4. One vocabulary on desktop and phone; the phone is not a reduced version.
