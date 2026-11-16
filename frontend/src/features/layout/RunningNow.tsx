import clsx from "clsx";
import { useTranslation } from "react-i18next";
import { useRuns, useTasks } from "../../api/queries";
import type { Run } from "../../api/types";
import { AgentDot } from "../../components/ui";
import { useSince } from "../../lib/time";
import { useShell } from "./shell-context";

/** The running turn of each agent, by agent id: where "working since" comes from. */
export function useRunningRuns(projectId: number): Map<number, Run> {
  const runs = useRuns(projectId).data ?? [];
  return new Map(runs.filter((r) => r.status === "running").map((r) => [r.agent_id, r]));
}

function useRows() {
  const { project, agents } = useShell();
  const tasks = useTasks(project.id).data ?? [];
  const running = useRunningRuns(project.id);
  const since = useSince();
  return agents.map((agent) => {
    const run = running.get(agent.id);
    const task = tasks.find((x) => (run?.task_id ? x.id === run.task_id : agent.is_manager ? x.manager_active : x.active_agent_ids.includes(agent.id)));
    return { agent, ticket: agent.status !== "idle" && task ? `#${task.number}` : "", time: run ? since(run.started_at ?? run.created_at) : "" };
  });
}

/** Who is working on what and for how long: a table with fixed columns (sidebar) or one scrolling
 * line (phone). A row opens the chat with that agent. */
export function RunningNow({ variant }: { variant: "table" | "strip" }) {
  const { t } = useTranslation();
  const { openChat } = useShell();
  const rows = useRows();

  if (variant === "strip") {
    const busy = rows.filter((row) => row.agent.status !== "idle");
    if (!busy.length) return null;
    return (
      <div className="flex shrink-0 items-center gap-4 overflow-x-auto px-4 pb-2.5 whitespace-nowrap text-fg-2 [scrollbar-width:none]">
        {busy.map(({ agent, ticket, time }) => (
          <button key={agent.id} type="button" onClick={() => openChat(agent.id)} className="inline-flex items-center gap-1.5" title={t("shell.openChat", { name: agent.name })}>
            <AgentDot status={agent.status} />
            {[agent.name, ticket].filter(Boolean).join(" ")}
            {time && ` · ${time}`}
          </button>
        ))}
      </div>
    );
  }
  return (
    <section className="flex min-h-0 flex-col px-3.5">
      <h2 className="caps shrink-0 border-b-2 border-fg pb-1.5">{t("shell.runningNow")}</h2>
      <div className="scroll-thin min-h-0 overflow-y-auto">
        {rows.map(({ agent, ticket, time }) => {
          const idle = agent.status === "idle";
          return (
            <button
              key={agent.id}
              type="button"
              onClick={() => openChat(agent.id)}
              title={[t("shell.openChat", { name: agent.name }), t(`agentStatus.${agent.status}`), ...agent.job_commands].join("\n")}
              className="grid w-full grid-cols-[9px_minmax(0,1fr)_auto_auto] items-center gap-x-2 border-b border-line py-[7px] text-left hover:bg-sunk"
            >
              <AgentDot status={agent.status} />
              <span className={clsx("truncate", idle && "text-fg-3")}>{agent.name}</span>
              <span className="text-fg-2">{ticket}</span>
              <span className={clsx("min-w-7 text-right", idle ? "text-fg-3" : "text-fg-2")}>{idle ? t("agentStatus.idle").toLowerCase() : time}</span>
            </button>
          );
        })}
      </div>
    </section>
  );
}
