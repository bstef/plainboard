import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api, errorMessage } from "../../api/client";
import type { ProviderAccount } from "../../api/types";
import { Button } from "../../components/ui";

/** The ChatGPT subscription of an account, signed in on the runner with a device code. */
export function CodexSignIn({ accountId }: { accountId: number }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const KEY = ["accountSignIn", accountId];
  const url = `/accounts/${accountId}/signin`;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const account = useQuery({
    queryKey: KEY,
    queryFn: () => api.get<ProviderAccount>(url),
    refetchInterval: (query) => (query.state.data?.login_code ? 3000 : false),
  });

  const act = async (action: "login" | "logout") => {
    setBusy(true);
    setError(null);
    try {
      qc.setQueryData(KEY, await (action === "login" ? api.post<ProviderAccount>(url) : api.del<ProviderAccount>(url)));
      if (action === "logout") await qc.invalidateQueries({ queryKey: KEY });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const data = account.data;
  const problem = error ?? data?.login_error ?? (account.error ? errorMessage(account.error) : null);
  return (
    <div className="space-y-2 rounded-sm bg-sunk p-3 text-fg-2">
      {data?.login_code ? (
        <>
          <p>{t("settings.codexDeviceCode")}</p>
          <a href={data.login_url ?? "#"} target="_blank" rel="noreferrer" className="block font-medium break-all text-fg underline">
            {data.login_url}
          </a>
          <p className="font-mono font-semibold tracking-widest text-fg select-all">{data.login_code}</p>
          <div className="flex items-center gap-3">
            <span className="animate-pulse">{t("settings.codexWaiting")}</span>
            <Button size="sm" variant="ghost" onClick={() => act("logout")} loading={busy}>
              {t("settings.codexCancel")}
            </Button>
          </div>
        </>
      ) : (
        <div className="flex flex-wrap items-center gap-3">
          <span className="min-w-0 flex-1">{data?.signed_in ? t("settings.codexSignedIn", { detail: data.detail }) : account.isLoading ? t("settings.testing") : t("settings.codexSignedOut")}</span>
          <Button size="sm" onClick={() => act(data?.signed_in ? "logout" : "login")} loading={busy}>
            {data?.signed_in ? t("settings.codexSignOut") : t("settings.codexSignIn")}
          </Button>
        </div>
      )}
      {problem && <p className="text-you-fg">{problem}</p>}
    </div>
  );
}
