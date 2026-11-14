import { Check, Copy, GitBranch, KeyRound, ListChecks, Pencil, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { errorMessage } from "../../api/client";
import type { RepoAuth } from "../../api/types";
import { Button, Field, IconButton, Input, Select, Textarea } from "../../components/ui";
import { GitHubRepoPicker } from "./GitHubRepoPicker";

export interface RepoDraft {
  key: string | number;
  name: string;
  url: string;
  default_branch: string;
  description: string;
  auth: RepoAuth;
  token: string; // write-only: empty keeps the stored one
  token_user: string;
  has_token: boolean;
  ssh_private_key: string; // write-only: one's own key instead of a created one
  ssh_public_key: string;
  new_ssh_key: boolean;
}

// Not crypto.randomUUID: browsers only have it over HTTPS or on localhost.
let drafts = 0;

export const emptyRepo = (): RepoDraft => ({
  key: `new-${++drafts}`,
  name: "",
  url: "",
  default_branch: "",
  description: "",
  auth: "default",
  token: "",
  token_user: "",
  has_token: false,
  ssh_private_key: "",
  ssh_public_key: "",
  new_ssh_key: false,
});

const AUTHS: { value: RepoAuth; label: string; hint: string }[] = [
  { value: "default", label: "projects.repoAccessDefault", hint: "projects.repoAccessDefaultHint" },
  { value: "token", label: "projects.repoAccessToken", hint: "projects.repoAccessTokenHint" },
  { value: "ssh", label: "projects.repoAccessSsh", hint: "projects.repoAccessSshHint" },
];

/** How git reaches one repository: the general token, its own token, or its own SSH key. */
function AccessFields({ draft, onChange }: { draft: RepoDraft; onChange: (patch: Partial<RepoDraft>) => void }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    await navigator.clipboard.writeText(draft.ssh_public_key);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="space-y-2">
      <Field label={t("projects.repoAccess")} hint={t(AUTHS.find((a) => a.value === draft.auth)!.hint)}>
        <Select value={draft.auth} onChange={(e) => onChange({ auth: e.target.value as RepoAuth })}>
          {AUTHS.map((a) => (
            <option key={a.value} value={a.value}>
              {t(a.label)}
            </option>
          ))}
        </Select>
      </Field>
      {draft.auth === "token" && (
        <div className="grid gap-2 sm:grid-cols-2">
          <Input
            type="password"
            autoComplete="off"
            aria-label={t("projects.repoToken")}
            placeholder={draft.has_token ? t("projects.repoTokenStored") : t("projects.repoToken")}
            value={draft.token}
            onChange={(e) => onChange({ token: e.target.value })}
          />
          <Input autoComplete="off" placeholder={t("projects.repoTokenUser")} value={draft.token_user} onChange={(e) => onChange({ token_user: e.target.value })} />
        </div>
      )}
      {draft.auth === "ssh" && (
        <>
          {draft.ssh_public_key ? (
            <Field group label={t("projects.repoSshPublicKey")} hint={t("projects.repoSshPublicKeyHint")}>
              <div className="flex items-start gap-2">
                <code className="min-w-0 flex-1 rounded-md bg-white px-2 py-1.5 font-mono text-xs break-all ring-1 ring-zinc-200 dark:bg-zinc-900 dark:ring-zinc-700">
                  {draft.ssh_public_key}
                </code>
                <Button size="sm" icon={copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />} onClick={copy}>
                  {copied ? t("projects.repoSshCopied") : t("projects.repoSshCopy")}
                </Button>
              </div>
            </Field>
          ) : (
            <p className="text-xs text-zinc-500">{t("projects.repoSshOnSave")}</p>
          )}
          {draft.ssh_public_key && (
            <label className="flex items-center gap-2 text-xs text-zinc-600 dark:text-zinc-400">
              <input type="checkbox" checked={draft.new_ssh_key} onChange={(e) => onChange({ new_ssh_key: e.target.checked })} />
              {t("projects.repoSshNew")}
            </label>
          )}
          <details className="text-xs text-zinc-500">
            <summary className="cursor-pointer">{t("projects.repoSshOwn")}</summary>
            <Textarea
              rows={3}
              className="mt-2 font-mono text-xs"
              autoComplete="off"
              spellCheck={false}
              aria-label={t("projects.repoSshOwn")}
              placeholder={t("projects.repoSshOwnPlaceholder")}
              value={draft.ssh_private_key}
              onChange={(e) => onChange({ ssh_private_key: e.target.value })}
            />
          </details>
        </>
      )}
    </div>
  );
}

/** Editable list of repositories. Persistence is up to the caller (API or local draft list). */
export function RepositoriesEditor({
  repos,
  projectId,
  onAdd,
  onSave,
  onRemove,
}: {
  repos: RepoDraft[];
  projectId?: number;
  onAdd: (draft: RepoDraft) => Promise<void>;
  onSave: (draft: RepoDraft) => Promise<void>;
  onRemove: (draft: RepoDraft) => Promise<void>;
}) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState<RepoDraft | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);

  const submit = async () => {
    if (!editing?.url.trim()) return;
    setError(null);
    try {
      await (isNew ? onAdd(editing) : onSave(editing));
      setEditing(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  return (
    <div className="space-y-2">
      {repos.length > 0 && (
        <ul className="divide-y divide-zinc-100 rounded-lg ring-1 ring-zinc-200 dark:divide-zinc-800 dark:ring-zinc-800">
          {repos.map((repo) => (
            <li key={repo.key} className="flex items-center gap-2 px-3 py-2">
              <GitBranch className="size-3.5 shrink-0 text-zinc-400" />
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-medium">
                  {repo.name || t("projects.repoAutoName")}
                  {repo.default_branch && <span className="ml-1.5 text-xs font-normal text-zinc-400">@{repo.default_branch}</span>}
                  {repo.auth !== "default" && (
                    <span className="ml-1.5 text-xs font-normal text-zinc-400">· {t(repo.auth === "ssh" ? "projects.repoAccessBadgeSsh" : "projects.repoAccessBadgeToken")}</span>
                  )}
                </div>
                <div className="truncate font-mono text-xs text-zinc-500">{repo.url}</div>
              </div>
              {repo.ssh_public_key && (
                <IconButton label={t("projects.repoSshCopyKey")} onClick={() => navigator.clipboard.writeText(repo.ssh_public_key)}>
                  <KeyRound className="size-3.5" />
                </IconButton>
              )}
              <IconButton
                label={t("common.edit")}
                onClick={() => {
                  setIsNew(false);
                  setEditing({ ...repo });
                }}
              >
                <Pencil className="size-3.5" />
              </IconButton>
              <IconButton
                label={t("common.delete")}
                onClick={async () => {
                  if (!confirm(t("projects.confirmRemoveRepo", { name: repo.name || repo.url }))) return;
                  try {
                    await onRemove(repo);
                  } catch (err) {
                    setError(errorMessage(err));
                  }
                }}
              >
                <Trash2 className="size-3.5" />
              </IconButton>
            </li>
          ))}
        </ul>
      )}
      {editing ? (
        <div className="space-y-2 rounded-lg bg-zinc-50 p-3 dark:bg-zinc-800/40">
          <Input autoFocus placeholder="https://github.com/owner/repo.git" value={editing.url} onChange={(e) => setEditing({ ...editing, url: e.target.value })} className="font-mono" />
          <div className="grid gap-2 sm:grid-cols-2">
            <Input placeholder={t("projects.repoNamePlaceholder")} value={editing.name} onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
            <Input placeholder={t("projects.repoBranchPlaceholder")} value={editing.default_branch} onChange={(e) => setEditing({ ...editing, default_branch: e.target.value })} />
          </div>
          <Input placeholder={t("projects.repoDescriptionPlaceholder")} value={editing.description} onChange={(e) => setEditing({ ...editing, description: e.target.value })} />
          <AccessFields draft={editing} onChange={(patch) => setEditing({ ...editing, ...patch })} />
          <div className="flex gap-2">
            <Button size="sm" variant="primary" onClick={submit} disabled={!editing.url.trim()}>
              {isNew ? t("projects.addRepo") : t("common.save")}
            </Button>
            <Button size="sm" onClick={() => setEditing(null)}>
              {t("common.cancel")}
            </Button>
          </div>
        </div>
      ) : picking ? (
        <GitHubRepoPicker
          projectId={projectId}
          existing={repos}
          onClose={() => setPicking(false)}
          onPick={async (drafts) => {
            setError(null);
            try {
              for (const draft of drafts) await onAdd(draft);
            } catch (err) {
              setError(errorMessage(err));
            }
          }}
        />
      ) : (
        <div className="flex flex-wrap gap-1">
          <Button size="sm" variant="ghost" icon={<ListChecks className="size-3.5" />} onClick={() => setPicking(true)}>
            {t("projects.pickFromGithub")}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            icon={<Plus className="size-3.5" />}
            onClick={() => {
              setIsNew(true);
              setEditing(emptyRepo());
            }}
          >
            {t("projects.addRepoByUrl")}
          </Button>
        </div>
      )}
      {error && <p className="text-xs text-red-600">{error}</p>}
    </div>
  );
}
