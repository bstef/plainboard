import { useMemo } from "react";
import { useFlow } from "../../api/queries";
import type { FlowBranch, FlowLive, FlowRepo, FlowTrain } from "../../api/types";
import { useNow } from "../../lib/time";
import { useShell } from "../layout/shell-context";

export const ms = (iso: string | null | undefined) => (iso ? Date.parse(iso) : NaN);

/** Seconds as m:ss. */
export function clock(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** How long a check run of this repository takes: the median of the latest ones, else the target an owner set. */
export const typicalOf = (repo: FlowRepo | undefined): number | null => repo?.typical_check_seconds || repo?.check_target_seconds || null;

export const elapsedOf = (train: FlowTrain, now: number) => Math.max(0, (now - ms(train.started_at)) / 1000);

export interface FlowModel {
  live: FlowLive;
  now: number;
  repoOf: (id: number) => FlowRepo | undefined;
  /** The branch with this name, to know who wrote it. */
  branchByName: (repositoryId: number, name: string) => FlowBranch | undefined;
  /** The repository an agent's work belongs to: the one of the branch it repairs, else of its latest branch, else the first. */
  repoOfAgent: (agentId: number) => number | undefined;
}

/** The live state of the project's flow, with a clock that moves once a second (for the time a train has been checked). */
export function useFlowModel(): FlowModel | null {
  const { project } = useShell();
  const live = useFlow(project.id).data;
  const now = useNow(1000);
  return useMemo(() => {
    if (!live) return null;
    const branchByName = (repositoryId: number, name: string) => live.branches.find((b) => b.repository_id === repositoryId && b.branch === name);
    return {
      live,
      now,
      repoOf: (id: number) => live.repositories.find((r) => r.id === id),
      branchByName,
      repoOfAgent: (agentId: number) => {
        const work = live.work.find((w) => w.agent_id === agentId);
        const first = work?.branch?.split(", ")[0];
        const repair = first ? live.branches.find((b) => b.branch === first) : undefined;
        const own = [...live.branches].filter((b) => b.agent_id === agentId).sort((a, b) => ms(b.since) - ms(a.since))[0];
        return (repair ?? own)?.repository_id ?? live.repositories[0]?.id;
      },
    };
  }, [live, now]);
}
