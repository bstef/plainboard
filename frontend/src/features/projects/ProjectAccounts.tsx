import { useTranslation } from "react-i18next";
import { useAccounts, useMe } from "../../api/queries";
import type { Account, Project } from "../../api/types";

/** The accounts a project may choose from: all of them, or the ones an admin limited it to. */
export const allowedAccounts = (project: Pick<Project, "account_ids"> | null | undefined, accounts: Account[]) =>
  project?.account_ids?.length ? accounts.filter((a) => project.account_ids.includes(a.id)) : accounts;

/** The same choice, whatever order it was ticked in. */
export const sameAccounts = (a: number[], b: number[]) => a.length === b.length && a.every((id) => b.includes(id));

/** Which of the installation's subscriptions this project may use. An admin ticks them, and they are
    stored with the rest of the form; everyone else reads what applies. Nothing ticked: any. */
export function ProjectAccounts({ value, onChange }: { value: number[]; onChange: (ids: number[]) => void }) {
  const { t } = useTranslation();
  const me = useMe();
  const accounts = useAccounts().data ?? [];

  if (!me?.is_admin) {
    if (!value.length) return null;
    const names = accounts.filter((a) => value.includes(a.id)).map((a) => a.label);
    return <p className="text-fg-2">{t("projectAccounts.readonly", { names: names.join(", ") })}</p>;
  }
  if (accounts.length < 2 && !value.length) return null; // one subscription: nothing to choose between

  return (
    <div className="space-y-1">
      <span className="block font-medium text-fg-2">{t("projectAccounts.label")}</span>
      <ul className="space-y-0.5">
        {accounts.map((a) => (
          <li key={a.id}>
            <label className="flex cursor-pointer items-center gap-2 rounded-sm px-1.5 py-1 hover:bg-sunk">
              <input
                type="checkbox"
                checked={value.includes(a.id)}
                onChange={() => onChange(value.includes(a.id) ? value.filter((id) => id !== a.id) : [...value, a.id])}
              />
              <span className="min-w-0 truncate">{a.label}</span>
            </label>
          </li>
        ))}
      </ul>
      <p className="text-fg-3">{t("projectAccounts.hint")}</p>
    </div>
  );
}
