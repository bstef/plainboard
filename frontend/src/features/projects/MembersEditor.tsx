import { useQueryClient } from "@tanstack/react-query";
import { Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { qk, useDirectory, useMembers } from "../../api/queries";
import type { Member, ProjectRole } from "../../api/types";
import { ErrorText, IconButton, Select } from "../../components/ui";

export const ROLES: ProjectRole[] = ["viewer", "member", "owner"];

/** Who has a part in a project, and which. Saved right away. Owners only. */
export function MembersEditor({ projectId }: { projectId: number }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const members = useMembers(projectId);
  const directory = useDirectory();
  const [error, setError] = useState<unknown>(null);
  const inProject = new Set((members.data ?? []).map((m) => m.user_id));
  const others = (directory.data ?? []).filter((p) => !inProject.has(p.id));

  const run = async (action: () => Promise<Member[]>) => {
    setError(null);
    try {
      qc.setQueryData(qk.members(projectId), await action());
      qc.invalidateQueries({ queryKey: qk.people(projectId) });
      await qc.invalidateQueries({ queryKey: qk.projects });
    } catch (err) {
      setError(err);
    }
  };
  const setRole = (userId: number, role: ProjectRole) => run(() => api.put<Member[]>(`/projects/${projectId}/members/${userId}`, { role }));

  return (
    <div className="space-y-2">
      <ul className="divide-y divide-line rounded-sm ring-1 ring-line">
        {(members.data ?? []).map((m) => (
          <li key={m.user_id} className="flex items-center gap-2 px-3 py-1.5">
            <div className="min-w-0 flex-1 truncate">
              <span className="font-medium">{m.name}</span> <span className="text-fg-3">{m.username}</span>
            </div>
            <div className="w-32 shrink-0">
              <Select aria-label={t("members.role")} value={m.role} onChange={(e) => setRole(m.user_id, e.target.value as ProjectRole)}>
                {ROLES.map((role) => (
                  <option key={role} value={role}>
                    {t(`members.roles.${role}`)}
                  </option>
                ))}
              </Select>
            </div>
            <IconButton label={t("members.remove")} onClick={() => run(() => api.del<Member[]>(`/projects/${projectId}/members/${m.user_id}`))}>
              <Trash2 className="size-4" />
            </IconButton>
          </li>
        ))}
        {members.data?.length === 0 && <li className="px-3 py-2 text-fg-3">{t("members.none")}</li>}
      </ul>
      {others.length > 0 ? (
        <Select aria-label={t("members.add")} value="" onChange={(e) => e.target.value && setRole(Number(e.target.value), "member")}>
          <option value="">{t("members.add")}</option>
          {others.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name} ({p.username})
            </option>
          ))}
        </Select>
      ) : (
        <p className="text-fg-3">{t("members.nobodyElse")}</p>
      )}
      <p className="text-fg-3">{t("members.rolesHint")}</p>
      <ErrorText error={error} />
    </div>
  );
}
