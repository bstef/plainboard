import { useSortable } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import clsx from "clsx";
import { Globe, MessageSquare, Paperclip } from "lucide-react";
import type { CSSProperties, ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { Priority, Task } from "../../api/types";
import { AgentSquare } from "../../components/ui";
import { useSince } from "../../lib/time";
import { useRunningRuns } from "../layout/RunningNow";
import { useShell } from "../layout/shell-context";

/** Priority is rank, so it is told by weight and case, not by colour. Medium is not shown. */
export const priorityStyle: Record<Priority, string> = {
  low: "text-fg-3",
  medium: "text-fg-2",
  high: "font-semibold text-fg",
  urgent: "caps font-extrabold text-fg",
};

/** OKLCH hue per label colour; lightness and chroma come from the theme (`--sq-l`, `--sq-c`). */
const labelHues: Record<string, number> = { red: 25, orange: 50, amber: 80, green: 150, teal: 190, blue: 250, indigo: 275, purple: 305, pink: 350 };

/** A label: its colour as a small square, its name as text. */
export function Label({ colors, label, className }: { colors: Record<string, string>; label: string; className?: string }) {
  const hue = labelHues[colors[label]];
  return (
    <span className={clsx("inline-flex items-center gap-1.5 text-fg-2", className)}>
      <span className={clsx("size-[7px] shrink-0 rounded-[1px]", hue === undefined ? "bg-fg-3" : "sq-hue")} style={hue === undefined ? undefined : ({ "--h": hue } as CSSProperties)} />
      {label}
    </span>
  );
}

/** One ticket as a ruled row: state mark in a fixed cell, number and title, what waits for the
 * user, then one line of labels and counts with a fixed cell at its right for who works on the
 * ticket and for how long. */
export function TaskCard({ task, overlay, done }: { task: Task; overlay?: boolean; done?: boolean }) {
  const { t } = useTranslation();
  const { project, openTask, agentById } = useShell();
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: `t-${task.id}`, disabled: overlay });
  const running = useRunningRuns(project.id);
  const since = useSince();

  const working = task.active_agent_ids.map((id) => agentById(id)).filter((a) => a !== undefined);
  const forYou = !!task.user_action || task.open_decisions > 0;
  const mark = forYou ? "mk-you" : working.length > 0 ? "mk-run" : task.manager_active ? "mk-plan" : done ? "mk-done" : "";
  // The turn on this ticket: a member working on it, or the manager planning it.
  const run = [...running.values()].find((r) => r.task_id === task.id) ?? working.map((a) => running.get(a.id)).find((r) => r !== undefined);
  const active = working.length > 0 || task.manager_active;

  const counts = (task.comment_count > 0 || task.attachment_count > 0 || !!task.customer_title) && (
    <>
      {task.comment_count > 0 && (
        <span className="inline-flex items-center gap-1 text-fg-3">
          <MessageSquare className="size-3.5" />
          {task.comment_count}
        </span>
      )}
      {task.attachment_count > 0 && (
        <span className="inline-flex items-center gap-1 text-fg-3">
          <Paperclip className="size-3.5" />
          {task.attachment_count}
        </span>
      )}
      {task.customer_title && (
        <span className="inline-flex text-fg-3" title={task.customer_title}>
          <Globe className="size-3.5" />
        </span>
      )}
    </>
  );
  // Counts stay on the line of the last label, so they never stand alone.
  const lastLabel = task.labels[task.labels.length - 1];
  const hasMeta = task.labels.length > 0 || task.priority !== "medium" || !!counts;

  return (
    <article
      ref={setNodeRef}
      style={{ transform: CSS.Translate.toString(transform), transition }}
      {...attributes}
      {...listeners}
      onClick={() => !overlay && openTask(task.id)}
      className={clsx(
        "grid cursor-pointer grid-cols-[9px_minmax(0,1fr)] gap-x-2 border-b border-line px-4 py-3 select-none md:px-2.5 md:py-[9px]",
        forYou ? "bg-you-bg" : overlay ? "bg-sunk" : "hover:bg-sunk",
        isDragging && "opacity-40",
        overlay && "shadow-lg ring-1 ring-line-2",
      )}
    >
      <span className={clsx("mk mt-[7px] md:mt-[6px]", mark)} title={task.manager_active && !working.length ? t("board.managerPlanning") : undefined} />
      <div className="min-w-0">
        <h3 className="font-medium text-balance break-words">
          <span className="mr-1.5 font-normal text-fg-3">#{task.number}</span>
          {task.title}
        </h3>
        {task.user_action && (
          <p className="mt-1 break-words">
            <ForYouPlate>{t("board.userAction")}</ForYouPlate>
            <span className="line-clamp-4">{task.user_action}</span>
          </p>
        )}
        {task.open_decisions > 0 && (
          <p className="mt-1">
            <ForYouPlate>{t("decision.title")}</ForYouPlate>
          </p>
        )}
        {(hasMeta || active) && (
          <div className="mt-0.5 flex items-end gap-2">
            <p className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2.5 gap-y-0.5">
              {task.priority !== "medium" && <span className={priorityStyle[task.priority]}>{t(`priority.${task.priority}`)}</span>}
              {task.labels.slice(0, -1).map((label) => (
                <Label key={label} colors={project.label_colors} label={label} />
              ))}
              {(lastLabel || counts) && (
                <span className="inline-flex items-center gap-2.5 whitespace-nowrap">
                  {lastLabel && <Label colors={project.label_colors} label={lastLabel} />}
                  {counts}
                </span>
              )}
            </p>
            {active && (
              <span className="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap text-fg-2" title={working.map((a) => a.name).join(", ") || t("board.managerPlanning")}>
                {working.map((a) => (
                  <AgentSquare key={a.id} agent={a} />
                ))}
                {run ? since(run.started_at ?? run.created_at) : !working.length && t("shell.planning")}
              </span>
            )}
          </div>
        )}
      </div>
    </article>
  );
}

/** Reversed vermilion plate that names what waits for the user. */
export function ForYouPlate({ children }: { children: ReactNode }) {
  return <b className="caps mr-1.5 inline-block rounded-sm bg-you px-1.5 font-extrabold whitespace-nowrap text-on-you">{children}</b>;
}
