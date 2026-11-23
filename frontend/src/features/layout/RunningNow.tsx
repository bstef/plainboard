import clsx from "clsx";
import { useLayoutEffect, useRef, useState } from "react";
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
  return <RunningTable rows={rows} />;
}

type Row = ReturnType<typeof useRows>[number];

/** The sidebar table never scrolls: it lists everyone (busy first) when the stack has the height for it,
 * otherwise only the busy agents plus a count of the idle ones. The height is measured, not guessed. */
function RunningTable({ rows }: { rows: Row[] }) {
  const { t } = useTranslation();
  const { openChat } = useShell();
  const section = useRef<HTMLElement>(null);
  const probe = useRef<HTMLDivElement>(null);
  const [roomy, setRoomy] = useState(false);
  const sorted = [...rows].sort((a, b) => Number(a.agent.status === "idle") - Number(b.agent.status === "idle"));
  const busy = sorted.filter((row) => row.agent.status !== "idle");
  const idle = sorted.length - busy.length;

  useLayoutEffect(() => {
    const me = section.current;
    const stack = me?.closest<HTMLElement>("[data-part=side-stack]");
    if (!me || !stack) return;
    const measure = () => {
      const leaves = [...stack.children].flatMap((c) => (c.hasAttribute("data-stack-end") ? [...c.children] : [c])).filter((c) => c !== me) as HTMLElement[];
      const gap = parseFloat(getComputedStyle(stack).rowGap) || 0;
      const used = leaves.reduce((sum, c) => sum + c.offsetHeight, 0) + gap * leaves.length;
      const need = probe.current?.offsetHeight ?? 0;
      setRoomy(need <= stack.clientHeight - used);
    };
    measure();
    const ro = new ResizeObserver(measure);
    const watch = () => {
      ro.disconnect();
      ro.observe(stack);
      if (probe.current) ro.observe(probe.current);
      stack.querySelectorAll(":scope > *, :scope > [data-stack-end] > *").forEach((c) => c !== me && ro.observe(c));
    };
    watch();
    const mo = new MutationObserver(watch);
    mo.observe(stack, { childList: true, subtree: true });
    return () => {
      ro.disconnect();
      mo.disconnect();
    };
  }, [rows.length]);

  const shown = roomy ? sorted : busy;
  const line = "block w-full border-b border-line py-[7px] text-left text-fg-3";
  return (
    <section ref={section} className="relative flex shrink-0 flex-col px-3.5" data-part="running-now">
      <h2 className="caps shrink-0 rule-b pb-1.5">{t("shell.runningNow")}</h2>
      <div>
        {shown.map((row) => (
          <RowLine key={row.agent.id} row={row} onOpen={() => openChat(row.agent.id)} />
        ))}
        {!roomy && !busy.length && <p className={line}>{t("shell.nobodyWorking")}</p>}
        {!roomy && busy.length > 0 && idle > 0 && <p className={line}>{t("shell.idleMore", { count: idle })}</p>}
      </div>
      {/* Invisible copy of the full list: its height is what "enough room" is measured against. */}
      <div ref={probe} aria-hidden className="pointer-events-none invisible absolute inset-x-3.5 top-0 -z-10">
        <h2 className="caps rule-b pb-1.5">{t("shell.runningNow")}</h2>
        {sorted.map((row) => (
          <RowLine key={row.agent.id} row={row} />
        ))}
      </div>
    </section>
  );
}

function RowLine({ row: { agent, ticket, time }, onOpen }: { row: Row; onOpen?: () => void }) {
  const { t } = useTranslation();
  const idle = agent.status === "idle";
  const cls = "grid w-full grid-cols-[9px_minmax(0,1fr)_auto_auto] items-center gap-x-2 border-b border-line py-[7px] text-left hover:bg-sunk";
  const cells = (
    <>
      <AgentDot status={agent.status} />
      <span className={clsx("truncate", idle && "text-fg-3")}>{agent.name}</span>
      <span className="text-fg-2">{ticket}</span>
      <span className={clsx("min-w-7 text-right", idle ? "text-fg-3" : "text-fg-2")}>{idle ? t("agentStatus.idle").toLowerCase() : time}</span>
    </>
  );
  return onOpen ? (
    <button type="button" onClick={onOpen} title={[t("shell.openChat", { name: agent.name }), t(`agentStatus.${agent.status}`), ...agent.job_commands].join("\n")} className={cls}>
      {cells}
    </button>
  ) : (
    <div className={cls}>{cells}</div>
  );
}
