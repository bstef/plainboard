import { useQuery } from "@tanstack/react-query";
import { Database } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { useApiMutation } from "../../api/queries";
import type { DatabaseStatus } from "../../api/types";
import { Button, ErrorText } from "../../components/ui";

const GUIDE = "https://github.com/hthoene/plainboard/blob/main/docs/guide.md#postgresql";

/** Which database, and for an installation on SQLite the move to the bundled PostgreSQL with one click. */
export function DatabaseEditor() {
  const { t } = useTranslation();
  const status = useQuery({ queryKey: ["database"], queryFn: () => api.get<DatabaseStatus>("/database") });
  const [asked, setAsked] = useState(false);
  const move = useApiMutation(() => api.post("/database/move"), () => [["database"]]);
  const db = status.data;
  if (!db) return null;
  if (db.database === "postgresql") return <p>{t("settings.databasePostgres")}</p>;
  const guide = (
    <a href={GUIDE} target="_blank" rel="noreferrer" className="underline underline-offset-[3px] hover:decoration-2">
      {t("settings.databaseGuide")}
    </a>
  );
  const requested = asked || db.move === "requested";
  return (
    <div className="space-y-2">
      <p>{t("settings.databaseSqlite")}</p>
      {db.failed && !requested && <p className="text-you-fg">{t("settings.databaseFailed", { reason: db.failed })}</p>}
      {requested ? (
        <p role="status" className="text-fg-2">
          {t("settings.databaseRestarting")}
        </p>
      ) : db.move === "ready" ? (
        <>
          <p className="text-fg-3">
            {t("settings.databaseMoveHint")} {guide}
          </p>
          <Button
            icon={<Database className="size-3.5" />}
            loading={move.isPending}
            onClick={() => confirm(t("settings.databaseConfirm")) && move.mutate(undefined, { onSuccess: () => setAsked(true) })}
          >
            {t("settings.databaseMoveButton")}
          </Button>
          <ErrorText error={move.error} />
        </>
      ) : (
        <p className="text-fg-3">
          {t(db.move === "not_empty" ? "settings.databaseNotEmpty" : "settings.databaseMoveSteps")} {guide}
        </p>
      )}
    </div>
  );
}
