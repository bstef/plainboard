import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import { Check, Lock, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { Button, Input, Spinner } from "../../components/ui";
import { emptyRepo, type RepoDraft } from "./RepositoriesEditor";

interface GitHubRepo {
  full_name: string;
  name: string;
  clone_url: string;
  description: string | null;
  default_branch: string | null;
  private: boolean;
}

interface GitHubReposResult {
  ok: boolean;
  detail: string;
  repos: GitHubRepo[];
}

/** Searchable multi-select of all repositories the GitHub token can access. */
export function GitHubRepoPicker({
  projectId,
  existing,
  onPick,
  onClose,
}: {
  projectId?: number;
  existing: RepoDraft[];
  onPick: (drafts: RepoDraft[]) => Promise<void>;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const result = useQuery({
    queryKey: ["github-repos", projectId ?? 0],
    queryFn: () => api.get<GitHubReposResult>(`/github/repos${projectId ? `?project_id=${projectId}` : ""}`),
    staleTime: 60_000,
  });

  const normalize = (url: string) => url.toLowerCase().replace(/\.git$/, "");
  const taken = useMemo(() => new Set(existing.map((r) => normalize(r.url))), [existing]);
  const available = (result.data?.repos ?? []).filter((r) => !taken.has(normalize(r.clone_url)));
  const needle = query.trim().toLowerCase();
  const visible = available.filter((r) => !needle || r.full_name.toLowerCase().includes(needle) || (r.description ?? "").toLowerCase().includes(needle));

  const toggle = (fullName: string) =>
    setSelected((cur) => {
      const next = new Set(cur);
      if (next.has(fullName)) next.delete(fullName);
      else next.add(fullName);
      return next;
    });

  const add = async () => {
    // Folder names must be unique: fall back to owner-name when two repos share a name.
    const names = new Set(existing.map((r) => r.name.toLowerCase()));
    const drafts = available
      .filter((r) => selected.has(r.full_name))
      .map((r) => {
        let name = r.name;
        if (names.has(name.toLowerCase())) name = r.full_name.replace("/", "-");
        names.add(name.toLowerCase());
        return { ...emptyRepo(), name, url: r.clone_url, description: r.description ?? "" };
      });
    setBusy(true);
    try {
      await onPick(drafts);
      onClose();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2 rounded-sm bg-sunk p-3">
      {result.isLoading ? (
        <div className="flex items-center gap-2 py-4 text-fg-3">
          <Spinner /> {t("projects.loadingGithub")}
        </div>
      ) : !result.data?.ok ? (
        <p className="text-you-fg">{result.data?.detail === "No GitHub token configured" ? t("projects.githubNoToken") : result.data?.detail || String(result.error ?? "")}</p>
      ) : (
        <>
          <div className="relative">
            <Search className="pointer-events-none absolute top-2 left-2.5 size-4 text-fg-3" />
            <Input autoFocus className="pl-8" placeholder={t("projects.searchGithub", { count: available.length })} value={query} onChange={(e) => setQuery(e.target.value)} />
          </div>
          <ul className="scroll-thin max-h-64 overflow-y-auto rounded-sm bg-bg ring-1 ring-line">
            {visible.map((r) => {
              const checked = selected.has(r.full_name);
              return (
                <li key={r.full_name}>
                  <button
                    type="button"
                    onClick={() => toggle(r.full_name)}
                    className={clsx("flex w-full items-start gap-2.5 px-3 py-2 text-left hover:bg-sunk", checked && "bg-sunk")}
                  >
                    <span
                      className={clsx(
                        "mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-sm ring-1",
                        checked ? "plate ring-fg" : "ring-line-2",
                      )}
                    >
                      {checked && <Check className="size-3" />}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-1.5 font-medium">
                        <span className="truncate">{r.full_name}</span>
                        {r.private && <Lock className="size-3 shrink-0 text-fg-3" aria-label={t("projects.private")} />}
                      </span>
                      {r.description && <span className="block truncate text-fg-3">{r.description}</span>}
                    </span>
                  </button>
                </li>
              );
            })}
            {visible.length === 0 && <li className="px-3 py-4 text-center text-fg-3">{t("projects.noGithubMatches")}</li>}
          </ul>
          <details className="text-fg-3">
            <summary className="cursor-pointer">{t("projects.githubMissing")}</summary>
            <p className="pt-1">{t("projects.githubMissingHint")}</p>
          </details>
        </>
      )}
      <div className="flex gap-2">
        <Button size="sm" variant="primary" onClick={add} loading={busy} disabled={selected.size === 0}>
          {t("projects.addSelected", { count: selected.size })}
        </Button>
        <Button size="sm" onClick={onClose}>
          {t("common.cancel")}
        </Button>
      </div>
    </div>
  );
}
