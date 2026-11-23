import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useEffect, useSyncExternalStore } from "react";
import { qk } from "./queries";
import type { Agent, Conversation, Message } from "./types";

// --- streaming text store (live deltas per agent, not persisted) -----------------------------

type StreamState = Record<number, string>;
let streams: StreamState = {};
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

const streamStore = {
  append(agentId: number, text: string) {
    streams = { ...streams, [agentId]: (streams[agentId] ?? "") + text };
    emit();
  },
  clear(agentId: number) {
    if (!(agentId in streams)) return;
    const { [agentId]: _, ...rest } = streams;
    streams = rest;
    emit();
  },
  subscribe(listener: () => void) {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
};

export function useStreamingText(agentId: number | null): string {
  return useSyncExternalStore(streamStore.subscribe, () => (agentId === null ? "" : (streams[agentId] ?? "")));
}

// --- attachments being copied to the project's external runner (live, not persisted) --------

type Copying = { done: number; total: number };
let copying: Record<number, Copying> = {};
const copyListeners = new Set<() => void>();

function setCopying(pid: number, value: Copying) {
  const { [pid]: _, ...rest } = copying;
  copying = value.done < value.total ? { ...rest, [pid]: value } : rest;
  copyListeners.forEach((l) => l());
}

function subscribeCopying(listener: () => void) {
  copyListeners.add(listener);
  return () => copyListeners.delete(listener);
}

export function useCopying(pid: number): Copying | undefined {
  return useSyncExternalStore(subscribeCopying, () => copying[pid]);
}

// --- websocket event handling ----------------------------------------------------------------

interface ServerEvent {
  type: string;
  data: any;
}

function upsertMessage(qc: QueryClient, agentId: number | undefined, conversationId: number, msg: Message) {
  const caches = qc.getQueriesData<Conversation>({ queryKey: ["conversation"] });
  for (const [key, conv] of caches) {
    if (!conv || conv.id !== conversationId) continue;
    const exists = conv.messages.some((m) => m.id === msg.id);
    const messages = exists ? conv.messages.map((m) => (m.id === msg.id ? msg : m)) : [...conv.messages, msg];
    qc.setQueryData<Conversation>(key, { ...conv, messages });
    agentId = agentId ?? conv.agent_id;
  }
  if (agentId !== undefined && msg.role === "assistant" && msg.kind === "text") streamStore.clear(agentId);
}

// A run produces bursts of events (ticket, comment, assignment, run, ...). Each refetches whole lists,
// so equal invalidations within a short window are merged into one.
const pending = new Map<string, { qc: QueryClient; queryKey: readonly unknown[] }>();
let flushTimer: number | undefined;

function invalidateSoon(qc: QueryClient, queryKey: readonly unknown[]) {
  pending.set(JSON.stringify(queryKey), { qc, queryKey });
  if (flushTimer !== undefined) return;
  flushTimer = window.setTimeout(() => {
    flushTimer = undefined;
    const batch = [...pending.values()];
    pending.clear();
    for (const { qc, queryKey } of batch) void qc.invalidateQueries({ queryKey });
  }, 120);
}

function handle(qc: QueryClient, pid: number, event: ServerEvent) {
  const { type, data } = event;
  const [entity] = type.split(".");
  switch (entity) {
    case "task":
    case "comment":
    case "attachment":
    case "decision":
      invalidateSoon(qc, qk.tasks(pid));
      invalidateSoon(qc, ["task"]);
      break;
    case "todo":
      invalidateSoon(qc, ["todos"]);
      invalidateSoon(qc, qk.agents(pid));
      break;
    case "ownnote":
      invalidateSoon(qc, ["ownNotes"]);
      invalidateSoon(qc, qk.agents(pid));
      break;
    case "assignment":
      invalidateSoon(qc, qk.assignments(pid));
      invalidateSoon(qc, ["task"]);
      break;
    case "agent":
      if (type === "agent.upserted") {
        qc.setQueryData<Agent[]>(qk.agents(pid), (old) => {
          if (!old) return old;
          const exists = old.some((a) => a.id === data.id);
          return exists ? old.map((a) => (a.id === data.id ? data : a)) : [...old, data];
        });
      } else invalidateSoon(qc, qk.agents(pid));
      break;
    case "run":
      invalidateSoon(qc, qk.runs(pid));
      if (type === "run.upserted" && data.status !== "running" && data.status !== "queued") streamStore.clear(data.agent_id);
      break;
    case "message":
      if (type === "message.delta") streamStore.append(data.agent_id, data.text);
      else upsertMessage(qc, undefined, data.conversation_id, data as Message);
      break;
    case "conversation":
      qc.setQueryData<Conversation>(qk.conversation(data.agent_id), (old) => (old ? { ...old, context_tokens: data.context_tokens } : old));
      qc.setQueryData<Agent[]>(qk.agents(pid), (old) =>
        old?.map((a) => (a.id === data.agent_id ? { ...a, context_tokens: data.context_tokens } : a)),
      );
      break;
    case "note":
      invalidateSoon(qc, qk.notes(pid));
      break;
    case "schedule":
      invalidateSoon(qc, qk.schedules(pid));
      break;
    case "mcp":
      invalidateSoon(qc, qk.mcp(pid));
      break;
    case "repository":
      invalidateSoon(qc, qk.repos(pid));
      invalidateSoon(qc, qk.agents(pid));
      break;
    case "project":
      invalidateSoon(qc, qk.projects);
      break;
    case "pause":
      invalidateSoon(qc, qk.pause);
      break;
    case "uploads":
      setCopying(pid, data);
      break;
    case "mention":
      invalidateSoon(qc, qk.mentions);
      break;
  }
}

/** Keeps the query cache in sync with server events for one project. Reconnects with backoff. */
export function useProjectEvents(pid: number | null) {
  const qc = useQueryClient();
  useEffect(() => {
    if (pid === null) return;
    let socket: WebSocket | null = null;
    let closed = false;
    let retry = 0;
    let timer: number | undefined;
    let ping: number | undefined;

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${proto}://${location.host}/api/projects/${pid}/ws`);
      socket.onopen = () => {
        if (retry > 0) qc.invalidateQueries(); // resync everything after a reconnect
        retry = 0;
        ping = window.setInterval(() => socket?.readyState === WebSocket.OPEN && socket.send("ping"), 25_000);
      };
      socket.onmessage = (msg) => handle(qc, pid, JSON.parse(msg.data));
      socket.onclose = () => {
        window.clearInterval(ping);
        if (closed) return;
        retry += 1;
        timer = window.setTimeout(connect, Math.min(10_000, 500 * 2 ** retry));
      };
    };
    connect();
    return () => {
      closed = true;
      window.clearTimeout(timer);
      window.clearInterval(ping);
      socket?.close();
    };
  }, [pid, qc]);
}
