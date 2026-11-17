import { useQueryClient } from "@tanstack/react-query";
import { Check, Download, Upload } from "lucide-react";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, ApiError } from "../../api/client";
import { qk } from "../../api/queries";
import type { Project } from "../../api/types";
import { Button, ErrorText, Field, Input, Toggle } from "../../components/ui";

/** What an import answers: the new project, what came along, and which credentials did not. */
interface Imported {
  project: Project;
  counts: Record<string, number>;
  missing: string[];
}

/** The whole project as one file, to take it to another PlainBoard. Credentials travel only under a passphrase. */
export function ExportProject({ project }: { project: Project }) {
  const { t } = useTranslation();
  const [passphrase, setPassphrase] = useState("");
  const [chats, setChats] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const download = async () => {
    setBusy(true);
    setError(null);
    try {
      const resp = await fetch(`/api/projects/${project.id}/export`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ passphrase, chats }),
      });
      if (!resp.ok) throw new ApiError(resp.status, (await resp.json().catch(() => null))?.detail ?? resp.statusText);
      const link = document.createElement("a");
      link.href = URL.createObjectURL(await resp.blob());
      link.download = `${project.slug}.plainboard.zip`;
      link.click();
      URL.revokeObjectURL(link.href);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <Field label={t("move.passphrase")} hint={t("move.passphraseHint")}>
        <Input type="password" autoComplete="new-password" value={passphrase} onChange={(e) => setPassphrase(e.target.value)} />
      </Field>
      <div className="space-y-1">
        <Toggle checked={chats} onChange={setChats} label={t("move.chats")} />
        <p className="text-fg-3">{t("move.chatsHint")}</p>
      </div>
      <Button variant="primary" icon={<Download className="size-3.5" />} loading={busy} onClick={download}>
        {t("move.download")}
      </Button>
      <ErrorText error={error} />
      <p className="text-fg-3">{t("move.exportNote")}</p>
    </div>
  );
}

/** A project from a file another PlainBoard made. `onOpen`: the user wants to go to it. */
export function ImportProject({ onOpen }: { onOpen: (project: Project) => void }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const file = useRef<HTMLInputElement>(null);
  const [chosen, setChosen] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [done, setDone] = useState<Imported | null>(null);

  const upload = async () => {
    const picked = file.current?.files?.[0];
    if (!picked) return;
    setBusy(true);
    setError(null);
    try {
      const body = new FormData();
      body.append("file", picked);
      body.append("passphrase", passphrase);
      setDone(await api.post<Imported>("/projects/import", body));
      await qc.invalidateQueries({ queryKey: qk.projects });
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  if (done)
    return (
      <div className="space-y-3">
        <p role="status" className="flex items-center gap-2 font-semibold text-ok">
          <Check className="size-4" /> {t("move.imported", { name: done.project.name })}
        </p>
        <p className="text-fg-2">
          {Object.entries(done.counts)
            .map(([what, n]) => t(`move.count.${what}`, { count: n, defaultValue: `${n} ${what}` }))
            .join(", ")}
        </p>
        {done.missing.length > 0 && (
          <div>
            <p className="font-medium">{t("move.missing")}</p>
            <ul className="list-disc pl-5 text-fg-2">
              {done.missing.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </div>
        )}
        <p className="text-fg-3">{t("move.importedNote")}</p>
        <Button variant="primary" onClick={() => onOpen(done.project)}>
          {t("move.open")}
        </Button>
      </div>
    );

  return (
    <div className="space-y-4">
      <Field label={t("move.file")} hint={t("move.fileHint")}>
        <div className="flex items-center gap-2">
          <input ref={file} type="file" accept=".zip,application/zip" className="sr-only" tabIndex={-1} onChange={(e) => setChosen(e.target.files?.[0]?.name ?? "")} />
          <Button icon={<Upload className="size-3.5" />} onClick={() => file.current?.click()}>
            {t("move.choose")}
          </Button>
          <span className="min-w-0 truncate text-fg-2">{chosen}</span>
        </div>
      </Field>
      <Field label={t("move.passphrase")} hint={t("move.passphraseImportHint")}>
        <Input type="password" autoComplete="off" value={passphrase} onChange={(e) => setPassphrase(e.target.value)} />
      </Field>
      <Button variant="primary" loading={busy} disabled={!chosen} onClick={upload}>
        {t("move.import")}
      </Button>
      <ErrorText error={error} />
    </div>
  );
}
