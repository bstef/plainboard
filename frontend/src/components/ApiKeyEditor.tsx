import { useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../api/client";
import type { ApiKey } from "../api/types";
import { timeAgo } from "../lib/format";
import { Button, ErrorText, IconButton, Input } from "./ui";

/** Keys that are shown once and then only by their last characters: create, see when one was used, revoke.
 *  `path` lists and creates them, `revokePath` + id deletes one. */
export function ApiKeyEditor({ path, revokePath, placeholder }: { path: string; revokePath: string; placeholder: string }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const queryKey = ["api-keys", path];
  const keys = useQuery({ queryKey, queryFn: () => api.get<ApiKey[]>(path) });
  const [label, setLabel] = useState("");
  const [created, setCreated] = useState<ApiKey | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const change = async (action: () => Promise<void>) => {
    setError(null);
    try {
      await action();
      await qc.invalidateQueries({ queryKey });
    } catch (err) {
      setError(err);
    }
  };
  const create = () =>
    change(async () => {
      setCreated(await api.post<ApiKey>(path, { label: label.trim() }));
      setCopied(false);
      setLabel("");
    });
  const revoke = (key: ApiKey) => {
    if (!confirm(t("projects.publicApiConfirmRevoke", { name: key.label || t("projects.publicApiUnnamed") }))) return;
    change(async () => {
      await api.del(`${revokePath}/${key.id}`);
      if (created?.id === key.id) setCreated(null);
    });
  };
  const copy = async () => {
    if (!created?.key) return;
    await navigator.clipboard.writeText(created.key);
    setCopied(true);
  };

  return (
    <div className="space-y-2">
      {(keys.data?.length ?? 0) > 0 && (
        <ul className="divide-y divide-line rounded-sm ring-1 ring-line">
          {keys.data!.map((key) => (
            <li key={key.id} className="flex items-center gap-2 px-3 py-2">
              <KeyRound className="size-3.5 shrink-0 text-fg-3" />
              <div className="min-w-0 flex-1">
                <div className="truncate font-medium">
                  {key.label || t("projects.publicApiUnnamed")}
                  {key.hint && <span className="ml-1.5 font-mono font-normal text-fg-3">…{key.hint}</span>}
                </div>
                <div className="truncate text-fg-3">
                  {t("projects.publicApiCreated", { when: timeAgo(key.created_at) })} ·{" "}
                  {key.last_used_at ? t("projects.publicApiUsed", { when: timeAgo(key.last_used_at) }) : t("projects.publicApiNeverUsed")}
                </div>
              </div>
              <IconButton label={t("projects.publicApiRevoke")} onClick={() => revoke(key)}>
                <Trash2 className="size-3.5" />
              </IconButton>
            </li>
          ))}
        </ul>
      )}
      {created?.key && (
        <div className="space-y-1 rounded-sm bg-you-bg p-3">
          <p className="text-you-fg">{t("projects.publicApiCopy")}</p>
          <div className="flex gap-2">
            <Input readOnly value={created.key} onFocus={(e) => e.target.select()} className="font-mono" />
            <Button size="md" onClick={copy}>
              {copied ? t("projects.repoSshCopied") : t("projects.repoSshCopy")}
            </Button>
          </div>
        </div>
      )}
      <div className="flex gap-2">
        <Input
          value={label}
          maxLength={200}
          placeholder={placeholder}
          onChange={(e) => setLabel(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              create();
            }
          }}
        />
        <Button size="md" className="shrink-0" onClick={create}>
          {t("projects.publicApiCreate")}
        </Button>
      </div>
      <ErrorText error={error} />
    </div>
  );
}
