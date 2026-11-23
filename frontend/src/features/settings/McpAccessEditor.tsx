import { useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import type { PersonalToken } from "../../api/types";
import { timeAgo } from "../../lib/format";
import { Button, ErrorText, Field, IconButton, Input, Select, Textarea, Toggle } from "../../components/ui";

const EXPIRY = ["", "30", "90", "365"];

/** One's own tokens for MCP clients and the API (api/mcp_server.py): made here, shown once, with the
 *  clients' configuration filled in. `admin`: also the switch for the whole installation. */
export function McpAccessEditor({ enabled, admin, onToggle }: { enabled: boolean; admin: boolean; onToggle: (on: boolean) => Promise<void> }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const queryKey = ["personal-tokens"];
  const tokens = useQuery({ queryKey, queryFn: () => api.get<PersonalToken[]>("/tokens") });
  const [label, setLabel] = useState("");
  const [readOnly, setReadOnly] = useState(false);
  const [expiry, setExpiry] = useState("");
  const [created, setCreated] = useState<PersonalToken | null>(null);
  const [error, setError] = useState<unknown>(null);
  const url = `${window.location.origin}/mcp`;
  const secret = created?.token ?? "<token>";

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
      setCreated(await api.post<PersonalToken>("/tokens", { label: label.trim(), read_only: readOnly, expires_in_days: expiry ? Number(expiry) : null }));
      setLabel("");
    });
  const revoke = (token: PersonalToken) => {
    if (!confirm(t("mcp.confirmRevoke", { name: token.label || t("mcp.unnamed") }))) return;
    change(async () => {
      await api.del(`/tokens/${token.id}`);
      if (created?.id === token.id) setCreated(null);
    });
  };
  const snippets: [string, string][] = [
    ["Claude Code", `claude mcp add --transport http plainboard ${url} --header "Authorization: Bearer ${secret}"`],
    ["Claude Desktop (claude_desktop_config.json)", JSON.stringify({ mcpServers: { plainboard: { command: "npx", args: ["-y", "mcp-remote", url, "--header", `Authorization: Bearer ${secret}`] } } }, null, 2)],
    [t("mcp.generic"), JSON.stringify({ mcpServers: { plainboard: { type: "http", url, headers: { Authorization: `Bearer ${secret}` } } } }, null, 2)],
  ];

  return (
    <div className="space-y-6">
      <p className="text-fg-3">{t("mcp.intro")}</p>
      {admin && (
        <div className="space-y-1">
          <Toggle checked={enabled} onChange={(on) => change(() => onToggle(on))} label={t("mcp.enabled")} />
          <p className="text-fg-3">{t("mcp.enabledHint")}</p>
        </div>
      )}
      {!enabled ? (
        <p className="text-you-fg">{t("mcp.off")}</p>
      ) : (
        <>
          <section className="space-y-2">
            <h3 className="caps rule-b pb-1.5">{t("mcp.tokens")}</h3>
            {(tokens.data?.length ?? 0) > 0 && (
              <ul className="divide-y divide-line rounded-sm ring-1 ring-line">
                {tokens.data!.map((token) => (
                  <li key={token.id} className="flex items-center gap-2 px-3 py-2">
                    <KeyRound className="size-3.5 shrink-0 text-fg-3" />
                    <div className="min-w-0 flex-1">
                      <div className="truncate font-medium">
                        {token.label || t("mcp.unnamed")}
                        <span className="ml-1.5 font-mono font-normal text-fg-3">…{token.hint}</span>
                        {token.read_only && <span className="ml-1.5 font-normal text-fg-3">· {t("mcp.readOnly")}</span>}
                      </div>
                      <div className="truncate text-fg-3">
                        {t("projects.publicApiCreated", { when: timeAgo(token.created_at) })} ·{" "}
                        {token.last_used_at ? t("projects.publicApiUsed", { when: timeAgo(token.last_used_at) }) : t("projects.publicApiNeverUsed")}
                        {token.expires_at && ` · ${t("mcp.expires", { date: new Date(token.expires_at).toLocaleDateString() })}`}
                      </div>
                    </div>
                    <IconButton label={t("projects.publicApiRevoke")} onClick={() => revoke(token)}>
                      <Trash2 className="size-3.5" />
                    </IconButton>
                  </li>
                ))}
              </ul>
            )}
            {created?.token && <p className="rounded-sm bg-you-bg p-3 text-you-fg">{t("mcp.copy")}</p>}
            <div className="flex flex-wrap items-center gap-2">
              <Input value={label} maxLength={200} placeholder={t("mcp.label")} onChange={(e) => setLabel(e.target.value)} className="min-w-48 flex-1" />
              <Select value={expiry} onChange={(e) => setExpiry(e.target.value)} aria-label={t("mcp.expiry")}>
                {EXPIRY.map((days) => (
                  <option key={days} value={days}>
                    {days ? t("mcp.days", { count: Number(days) }) : t("mcp.noExpiry")}
                  </option>
                ))}
              </Select>
              <Toggle checked={readOnly} onChange={setReadOnly} label={t("mcp.readOnly")} />
              <Button size="md" className="shrink-0" onClick={create}>
                {t("mcp.create")}
              </Button>
            </div>
            <ErrorText error={error} />
          </section>
          <section className="space-y-3">
            <h3 className="caps rule-b pb-1.5">{t("mcp.clients")}</h3>
            {snippets.map(([name, text]) => (
              <Field key={name} label={name}>
                <Textarea readOnly value={text} rows={text.split("\n").length} onFocus={(e) => e.target.select()} className="font-mono" />
              </Field>
            ))}
            <p className="text-fg-3">{t("mcp.clientsHint", { url })}</p>
          </section>
        </>
      )}
    </div>
  );
}
