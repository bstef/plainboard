import clsx from "clsx";
import { AppWindow, CircleStop, ExternalLink, Globe, KeyRound } from "lucide-react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { qk, useApiMutation, usePreviews } from "../../api/queries";
import type { Agent, Preview } from "../../api/types";
import { Badge, Button, ErrorText, IconButton } from "../../components/ui";

import { useSince } from "../../lib/time";
import { useShell } from "../layout/shell-context";

/** What a preview is called: the title of its page, else where it runs. */
const nameOf = (p: Preview) => p.title || `localhost:${p.port}`;
/** Where PlainBoard serves it, for a tab of the user's own browser. */
const tabOf = (p: Preview) => `/${p.base ? "preview-base" : "preview"}/${p.port}/`;
/** Where it is inside the agents' environment, for the browser window. A server with a base path answers only under it. */
const addressOf = (p: Preview) => `http://localhost:${p.port}${p.base ? `/preview-base/${p.port}/` : "/"}`;

/** The servers the project's agents have left running. Only asked for while a job runs at all. */
export function useActivePreviews(projectId: number, agents: Agent[]): Preview[] {
  const running = agents.some((a) => a.running_jobs > 0);
  const previews = usePreviews(projectId, running);
  return running ? (previews.data ?? []) : [];
}

/** The running previews in the sidebar: a row opens the page in a tab, the icon in the browser window. */
export function PreviewsSide() {
  const { t } = useTranslation();
  const { previews, canWrite, openBrowser } = useShell();
  if (!previews.length) return null;
  const row = "flex min-w-0 flex-1 items-center gap-x-2 py-[7px] text-left";
  return (
    <section className="shrink-0 px-3.5">
      <h2 className="caps rule-b pb-1.5">{t("previews.title")}</h2>
      <ul className="scroll-thin max-h-44 overflow-y-auto">
        {previews.map((p) => {
          const body = (
            <>
              <span className="mk mk-run" />
              <span className="truncate">{nameOf(p)}</span>
            </>
          );
          const about = `localhost:${p.port}\n${p.command}`;
          return (
            <li key={`${p.job_id}-${p.port}`} className="group flex items-center border-b border-line hover:bg-sunk">
              {!p.local ? (
                <a href={tabOf(p)} target="_blank" rel="noreferrer" title={`${t("browser.openTab")}\n${about}`} className={row}>
                  {body}
                </a>
              ) : canWrite ? (
                <button type="button" onClick={() => openBrowser(addressOf(p))} title={`${t("browser.openHere")}\n${about}`} className={row}>
                  {body}
                </button>
              ) : (
                <span title={about} className={row}>
                  {body}
                </span>
              )}
              {/* The port tells two servers with the same title apart. Under the pointer it gives way to the second way in. */}
              <span className={clsx("pl-2 text-fg-3", canWrite && !p.local && "group-focus-within:hidden group-hover:hidden")}>{p.port}</span>
              {canWrite && !p.local && (
                <span className="hidden group-focus-within:block group-hover:block">
                  <IconButton label={t("browser.openHere")} onClick={() => openBrowser(addressOf(p))}>
                    <AppWindow className="size-3.5" />
                  </IconButton>
                </span>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/** The running previews with everything about them: who started which server, how it is shared, and
 * the ways to open or stop it. `onOpenHere`: the browser window is ready to show one. */
export function PreviewList({ onOpenHere }: { onOpenHere?: (address: string) => void }) {
  const { t } = useTranslation();
  const { project, previews, canWrite, agentById } = useShell();
  const since = useSince();
  const stop = useApiMutation(
    (p: Preview) => api.post(`/projects/${project.id}/previews/${p.job_id}/stop`),
    () => [qk.previews(project.id), qk.agents(project.id)],
  );
  if (!previews.length) return null;
  return (
    <section className="w-full rounded-sm bg-bg px-4 pt-3 pb-1 ring-1 ring-line">
      <h2 className="caps rule-b pb-1.5">{t("previews.running")}</h2>
      <ul>
        {previews.map((p) => (
          <li key={`${p.job_id}-${p.port}`} className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-line py-3 last:border-b-0">
            <div className="min-w-0 flex-1 basis-56">
              <div className="flex min-w-0 items-center gap-2">
                <span className="mk mk-run" />
                <span className="truncate font-semibold">{nameOf(p)}</span>
                {p.access !== "private" && (
                  <Badge className="shrink-0">
                    {p.access === "public" ? <Globe className="size-3.5" /> : <KeyRound className="size-3.5" />}
                    {t(`previews.access.${p.access}`)}
                  </Badge>
                )}
              </div>
              <div className="truncate pl-[17px] text-fg-2">{[`localhost:${p.port}`, agentById(p.agent_id)?.name, since(p.started_at)].filter(Boolean).join(" · ")}</div>
              <code className="block truncate pl-[17px] text-fg-3" title={p.command}>
                {p.command}
              </code>
              {p.local && <p className="pl-[17px] text-fg-2">{t("previews.localOnly")}</p>}
            </div>
            <div className="flex shrink-0 items-center gap-1.5">
              {onOpenHere && (
                <Button size="sm" icon={<AppWindow className="size-3.5" />} onClick={() => onOpenHere(addressOf(p))}>
                  {t("previews.openHere")}
                </Button>
              )}
              {!p.local && (
                <a
                  href={tabOf(p)}
                  target="_blank"
                  rel="noreferrer"
                  data-part="button"
                  data-variant="secondary"
                  className="inline-flex h-7 shrink-0 items-center justify-center gap-1.5 rounded-sm px-2.5 font-medium whitespace-nowrap text-fg ring-1 ring-line-2 ring-inset hover:bg-sunk touch:h-11"
                >
                  <ExternalLink className="size-3.5" />
                  {t("previews.openTab")}
                </a>
              )}
              {canWrite && (
                <Button
                  size="sm"
                  variant="ghost"
                  title={t("previews.stop")}
                  disabled={stop.isPending}
                  icon={<CircleStop className="size-3.5" />}
                  onClick={() => confirm(t("previews.confirmStop", { name: nameOf(p) })) && stop.mutate(p)}
                >
                  {t("previews.stopShort")}
                </Button>
              )}
            </div>
          </li>
        ))}
      </ul>
      <ErrorText error={stop.error} />
    </section>
  );
}
