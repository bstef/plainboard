import clsx from "clsx";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useAssignments, useTasks } from "../../api/queries";
import type { Agent } from "../../api/types";
import { AgentSquare } from "../../components/ui";
import { timeAgo } from "../../lib/format";
import { useSince } from "../../lib/time";
import { useShell } from "../layout/shell-context";
import { LimitsList } from "./LimitsRing";

const FINISHED_SHOWN = 4;

/** What surrounds the conversation, next to it on wide screens: the tickets that wait for the
 * user, what the agent is working on (for the manager: what it has handed out), and the plan
 * limits. Each row opens its ticket. */
export function ChatAside({ agent }: { agent: Agent }) {
  const { t } = useTranslation();
  const { project, openTask, agentById } = useShell();
  const tasks = useTasks(project.id).data ?? [];
  const assignments = useAssignments(project.id).data ?? [];
  const since = useSince();

  const waiting = tasks.filter((task) => task.open_decisions > 0 || task.user_action).sort((a, b) => a.number - b.number);
  const relevant = assignments.filter((a) => (agent.is_manager ? a.created_by_agent_id === agent.id : a.agent_id === agent.id));
  const active = relevant.filter((a) => a.status === "running" || a.status === "queued").sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at));
  const finished = relevant
    .filter((a) => a.finished_at)
    .sort((a, b) => Date.parse(b.finished_at!) - Date.parse(a.finished_at!))
    .slice(0, FINISHED_SHOWN);
  const number = (taskId: number | null) => tasks.find((task) => task.id === taskId)?.number;
  const row = "grid w-full grid-cols-[9px_minmax(0,1fr)_auto] items-baseline gap-x-2 border-b border-line px-2.5 py-2 text-left";

  return (
    <aside className="scroll-thin flex w-72 shrink-0 flex-col overflow-y-auto border-l border-line 2xl:w-80">
      <Section title={t("board.userAction")} count={waiting.length}>
        {waiting.length === 0 && <p className="px-2.5 py-2 text-fg-3">{t("chat.aside.nothingWaits")}</p>}
        {waiting.map((task) => (
          <button key={task.id} type="button" onClick={() => openTask(task.id)} className={clsx(row, "bg-you-bg hover:bg-sunk")}>
            <span className="mk mk-you self-center" />
            <span className="min-w-0">
              <span className="block truncate font-medium">
                <span className="mr-1.5 font-normal text-fg-3">#{task.number}</span>
                {task.title}
              </span>
              <span className="line-clamp-3 text-fg-2">{task.user_action || t("decision.title")}</span>
            </span>
          </button>
        ))}
      </Section>

      <Section title={t(agent.is_manager ? "chat.aside.delegated" : "ticket.assignments")} count={active.length}>
        {active.length + finished.length === 0 && <p className="px-2.5 py-2 text-fg-3">{t("chat.aside.noAssignments")}</p>}
        {[...active, ...finished].map((a) => {
          const member = agentById(a.agent_id);
          const ticket = number(a.task_id);
          const open = a.status === "running" || a.status === "queued";
          return (
            <button key={a.id} type="button" disabled={a.task_id === null} onClick={() => a.task_id !== null && openTask(a.task_id)} className={clsx(row, "enabled:hover:bg-sunk")}>
              <span className={clsx("mk self-center", a.status === "running" ? "mk-run" : a.status === "queued" ? "mk-plan" : a.status === "done" ? "mk-done" : "")} />
              <span className="min-w-0">
                <span className={clsx("block truncate", open ? "font-medium" : "text-fg-2")}>
                  {ticket !== undefined && <span className="mr-1.5 font-normal text-fg-3">#{ticket}</span>}
                  {a.title}
                </span>
                <span className="flex items-center gap-1.5 text-fg-3">
                  {agent.is_manager && member && <AgentSquare agent={member} />}
                  <span className="truncate">{[agent.is_manager ? member?.name : null, t(`assignment.${a.status}`)].filter(Boolean).join(" · ")}</span>
                </span>
              </span>
              <span className="text-fg-3">{open ? since(a.created_at) : timeAgo(a.finished_at)}</span>
            </button>
          );
        })}
      </Section>

      <Section title={t("chat.aside.limits")}>
        <LimitsList />
      </Section>
    </aside>
  );
}

function Section({ title, count, children }: { title: string; count?: number; children: ReactNode }) {
  return (
    <section className="shrink-0 pb-5">
      <h2 className="caps mx-2.5 flex h-12 items-center rule-b">
        <span className="min-w-0 flex-1 truncate">{title}</span>
        {count !== undefined && count > 0 && <span className="font-normal text-fg-3">{count}</span>}
      </h2>
      {children}
    </section>
  );
}
