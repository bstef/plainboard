import clsx from "clsx";
import { GitBranch } from "lucide-react";
import { AgentSquare } from "../../components/ui";
import type { Agent } from "../../api/types";

export function Who({ agent, className }: { agent?: Agent; className?: string }) {
  if (!agent) return null;
  return (
    <span className={clsx("inline-flex min-w-0 items-center gap-1.5", className)}>
      <AgentSquare agent={agent} />
      <span className="truncate">{agent.name}</span>
    </span>
  );
}

export function Branch({ name, className }: { name: string; className?: string }) {
  return (
    <span data-part="branch" className={clsx("flex min-w-0 max-w-full items-center gap-1 font-mono text-fg-2", className)}>
      <GitBranch className="size-3.5 shrink-0 text-fg-3" />
      <span className="truncate">{name}</span>
    </span>
  );
}

/** Time against the typical duration of a check run. The bar stays full when it takes longer. */
export function Progress({ elapsed, typical, className }: { elapsed: number; typical: number; className?: string }) {
  const pct = Math.min(100, (elapsed / Math.max(typical, 1)) * 100);
  return (
    <div className={clsx("h-1.5 w-full overflow-hidden rounded-sm bg-line", className)} data-part="progress" role="progressbar" aria-valuenow={Math.round(pct)}>
      <div className="flow-stripes h-full rounded-sm bg-fg-2" style={{ width: `${pct}%` }} />
    </div>
  );
}

/** The state mark of DESIGN.md's fixed cell: filled = working, hollow = waits or in the background, diamond = needs someone, check = done. */
export function Mark({ kind }: { kind: "run" | "plan" | "done" | "you" }) {
  return <span className={clsx("mk", kind === "run" && "mk-run", kind === "plan" && "mk-plan", kind === "done" && "mk-done", kind === "you" && "mk-you")} />;
}
