import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { qk, useAccounts, useMe } from "../../api/queries";
import type { Account, Project } from "../../api/types";
import { ErrorText } from "../../components/ui";

/** The accounts a project may choose from: all of them, or the ones an admin limited it to. */
export const allowedAccounts = (project: Pick<Project, "account_ids"> | null | undefined, accounts: Account[]) =>
  project?.account_ids?.length ? accounts.filter((a) => project.account_ids.includes(a.id)) : accounts;

/** Which of the installation's subscriptions this project may use. An admin ticks them (stored at
    once); everyone else reads what applies. Nothing ticked: any. */
export function ProjectAccounts({ project }: { project: Project }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const me = useMe();
  const accounts = useAccounts().data ?? [];
  const [error, setError] = useState<unknown>(null);
  const chosen = project.account_ids ?? [];

  if (!me?.is_admin) {
    if (!chosen.length) return null;
    const names = accounts.filter((a) => chosen.includes(a.id)).map((a) => a.label);
    return <p className="text-fg-2">{t("projectAccounts.readonly", { names: names.join(", ") })}</p>;
  }
  if (accounts.length < 2 && !chosen.length) return null; // one subscription: nothing to choose between

  const toggle = async (id: number) => {
    setError(null);
    try {
      const account_ids = chosen.includes(id) ? chosen.filter((c) => c !== id) : [...chosen, id];
      await api.put(`/projects/${project.id}/accounts`, { account_ids });
      await qc.invalidateQueries({ queryKey: qk.projects });
    } catch (err) {
      setError(err);
    }
  };

  return (
    <div className="space-y-1">
      <span className="block font-medium text-fg-2">{t("projectAccounts.label")}</span>
      <ul className="space-y-0.5">
        {accounts.map((a) => (
          <li key={a.id}>
            <label className="flex cursor-pointer items-center gap-2 rounded-sm px-1.5 py-1 hover:bg-sunk">
              <input type="checkbox" checked={chosen.includes(a.id)} onChange={() => toggle(a.id)} />
              <span className="min-w-0 truncate">{a.label}</span>
            </label>
          </li>
        ))}
      </ul>
      <p className="text-fg-3">{t("projectAccounts.hint")}</p>
      <ErrorText error={error} />
    </div>
  );
}
