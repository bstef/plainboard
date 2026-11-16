import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { CheckCircle2, XCircle } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { api, errorMessage } from "../../api/client";
import { checks, qk, useMeta, useSettings } from "../../api/queries";
import type { CheckResult } from "../../api/types";
import { Button, ErrorText, Field, Input, Modal, Select, Textarea, Toggle } from "../../components/ui";
import { equivalentModel } from "../../lib/models";
import { ModelSelect } from "../team/AgentEditor";
import { AccountsEditor } from "./AccountsEditor";
import { SecretsEditor } from "./SecretsEditor";

export function CheckBadge({ result }: { result: CheckResult | null }) {
  if (!result) return null;
  return (
    <span className={clsx("flex min-w-0 items-center gap-1", result.ok ? "text-ok" : "text-you-fg")}>
      {result.ok ? <CheckCircle2 className="size-3.5 shrink-0" /> : <XCircle className="size-3.5 shrink-0" />}
      <span className="truncate" title={result.detail}>
        {result.detail}
      </span>
    </span>
  );
}

type Values = Record<string, any>;

export function SettingsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const settings = useSettings();
  const meta = useMeta();
  const [values, setValues] = useState<Values>({});
  const [secrets, setSecrets] = useState<Values>({});
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [githubCheck, setGithubCheck] = useState<CheckResult | null>(null);

  useEffect(() => {
    if (open && settings.data) {
      setValues(settings.data.values);
      setSecrets({});
      setGithubCheck(null);
    }
  }, [open, settings.data]);

  if (!settings.data) return null;
  const set = (patch: Values) => setValues((v) => ({ ...v, ...patch }));
  const setSecret = (patch: Values) => setSecrets((s) => ({ ...s, ...patch }));
  const providers = meta.data?.providers ?? [];
  const provider = providers.find((p) => p.name === (values.default_provider ?? "claude_code"));
  const setDefaultProvider = (name: string) =>
    set({
      default_provider: name,
      default_manager_model: equivalentModel(providers, values.default_manager_model ?? "", name),
      default_worker_model: equivalentModel(providers, values.default_worker_model ?? "", name),
      default_summary_model: equivalentModel(providers, values.default_summary_model ?? "", name),
    });

  const persist = async () => {
    const editable = Object.fromEntries(Object.entries(values).filter(([k]) => !settings.data!.secrets.includes(k)));
    const changedSecrets = Object.fromEntries(Object.entries(secrets).filter(([, v]) => v !== undefined));
    await api.put("/settings", { values: { ...editable, ...changedSecrets } });
    setSecrets({});
    await qc.invalidateQueries({ queryKey: qk.settings });
  };

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await persist();
      onClose();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const checkGithub = async () => {
    setGithubCheck({ ok: true, detail: t("settings.testing") });
    try {
      await persist();
      setGithubCheck(await checks.github());
    } catch (err) {
      setGithubCheck({ ok: false, detail: errorMessage(err) });
    }
  };

  const secretField = (key: string, label: string, hint?: ReactNode) => (
    <Field
      group
      label={label}
      hint={
        <>
          {settings.data!.values[key] ? t("settings.secretSet") : settings.data!.env_fallbacks[key] ? t("settings.secretFromEnv") : t("settings.secretUnset")}
          {hint && <> · {hint}</>}
        </>
      }
    >
      <div className="flex gap-2">
        <Input type="password" autoComplete="off" placeholder="••••••••" value={secrets[key] ?? ""} onChange={(e) => setSecret({ [key]: e.target.value })} />
        {settings.data!.values[key] && (
          <Button size="md" variant="ghost" onClick={() => setSecret({ [key]: "" })}>
            {t("settings.clear")}
          </Button>
        )}
      </div>
    </Field>
  );

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t("settings.title")}
      footer={
        <>
          <Button onClick={onClose}>{t("common.cancel")}</Button>
          <Button variant="primary" onClick={save} loading={busy}>
            {t("common.save")}
          </Button>
        </>
      }
    >
      <div className="space-y-7">
        <Section title={t("settings.claude")} description={t("settings.claudeHint")}>
          <AccountsEditor provider="claude_code" />
        </Section>

        {providers.some((p) => p.name === "codex") && (
          <Section title={t("settings.codex")} description={t("settings.codexHint")}>
            <AccountsEditor provider="codex" />
          </Section>
        )}

        <Section title={t("settings.github")} description={t("settings.githubHint")}>
          {secretField("github_token", t("settings.githubToken"))}
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label={t("settings.gitName")}>
              <Input value={values.git_author_name ?? ""} onChange={(e) => set({ git_author_name: e.target.value })} />
            </Field>
            <Field label={t("settings.gitEmail")}>
              <Input value={values.git_author_email ?? ""} onChange={(e) => set({ git_author_email: e.target.value })} />
            </Field>
          </div>
          <div className="flex items-center gap-3">
            <Button size="sm" onClick={checkGithub}>
              {t("settings.test")}
            </Button>
            <CheckBadge result={githubCheck} />
          </div>
          <SshKeyEditor publicKey={settings.data.values.git_ssh_public_key ?? ""} />
        </Section>

        <Section title={t("secrets.title")} description={t("secrets.globalHint")}>
          <SecretsEditor projectId={null} />
        </Section>

        {/* What holds for the whole installation, at once: not a default that an agent could override. */}
        <Section title={t("settings.general")} description={t("settings.generalHint")}>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label={t("settings.maxParallel")} hint={t("settings.maxParallelHint")}>
              <Input type="number" min={0} max={20} value={values.max_parallel_runs ?? 3} onChange={(e) => set({ max_parallel_runs: Number(e.target.value) })} />
            </Field>
            <Field label={t("settings.managerParallel")} hint={t("settings.managerParallelHint")}>
              <Input type="number" min={0} max={20} value={values.manager_parallel_limit ?? 6} onChange={(e) => set({ manager_parallel_limit: Number(e.target.value) })} />
            </Field>
            <Field label={t("settings.timezone")}>
              <Input value={values.timezone ?? ""} onChange={(e) => set({ timezone: e.target.value })} placeholder="Europe/Berlin" />
            </Field>
          </div>
          <div className="space-y-1">
            <Toggle checked={values.update_check ?? true} onChange={(v) => set({ update_check: v })} label={t("settings.updateCheck")} />
            <p className="text-fg-3">
              {t("settings.updateCheckHint")}
              {meta.data?.version && ` ${t("settings.versionRunning", { commit: meta.data.version.commit })}`}
              {meta.data?.version?.behind ? ` ${t("update.how", { count: meta.data.version.behind })}` : meta.data?.version?.behind === 0 ? ` ${t("settings.versionCurrent")}` : ""}
            </p>
          </div>
        </Section>

        <Section title={t("settings.defaults")} description={t("settings.defaultsHint")}>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label={t("settings.defaultProvider")}>
              <Select value={values.default_provider ?? "claude_code"} onChange={(e) => setDefaultProvider(e.target.value)}>
                {providers.map((p) => (
                  <option key={p.name} value={p.name}>
                    {p.label}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label={t("settings.defaultEffort")}>
              <Select value={values.default_effort ?? ""} onChange={(e) => set({ default_effort: e.target.value || null })}>
                <option value="">{t("team.effortDefault")}</option>
                {(provider?.efforts ?? []).map((e) => (
                  <option key={e} value={e}>
                    {e}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label={t("settings.managerModel")}>
              <ModelSelect provider={provider} value={values.default_manager_model ?? ""} onChange={(v) => set({ default_manager_model: v })} />
            </Field>
            <Field label={t("settings.workerModel")}>
              <ModelSelect provider={provider} value={values.default_worker_model ?? ""} onChange={(v) => set({ default_worker_model: v })} />
            </Field>
            <Field label={t("settings.summaryModel")} hint={t("team.summaryModelHint")}>
              <ModelSelect provider={provider} value={values.default_summary_model ?? ""} onChange={(v) => set({ default_summary_model: v })} />
            </Field>
            <Field label={t("settings.contextLimit")} hint={t("team.contextLimitHint")}>
              <Input type="number" min={5000} step={5000} value={values.default_context_limit ?? 120000} onChange={(e) => set({ default_context_limit: Number(e.target.value) })} />
            </Field>
            <Field label={t("settings.keepRecent")}>
              <Input type="number" min={0} max={50} value={values.default_keep_recent ?? 6} onChange={(e) => set({ default_keep_recent: Number(e.target.value) })} />
            </Field>
          </div>
        </Section>
        <ErrorText error={error} />
      </div>
    </Modal>
  );
}

/** One SSH key for all repositories with an SSH address. Saved right away, apart from the form. */
function SshKeyEditor({ publicKey }: { publicKey: string }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [own, setOwn] = useState("");
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const change = async (action: () => Promise<unknown>) => {
    setError(null);
    try {
      await action();
      setOwn("");
      setCopied(false);
      await qc.invalidateQueries({ queryKey: qk.settings });
    } catch (err) {
      setError(err);
    }
  };
  const create = (privateKey?: string) => {
    if (publicKey && !confirm(t("settings.sshKeyConfirmNew"))) return;
    change(() => api.post("/settings/git-ssh-key", privateKey ? { private_key: privateKey } : {}));
  };
  const remove = () => {
    if (confirm(t("settings.sshKeyConfirmRemove"))) change(() => api.del("/settings/git-ssh-key"));
  };
  const copy = async () => {
    await navigator.clipboard.writeText(publicKey);
    setCopied(true);
  };

  return (
    <Field group label={t("settings.sshKey")} hint={t("settings.sshKeyHint")}>
      <div className="space-y-2">
        {publicKey ? (
          <code className="block rounded-sm bg-sunk px-2 py-1.5 font-mono break-all ring-1 ring-line">{publicKey}</code>
        ) : (
          <p className="text-fg-3">{t("settings.sshKeyNone")}</p>
        )}
        <div className="flex flex-wrap gap-2">
          {publicKey && (
            <Button size="sm" variant="primary" onClick={copy}>
              {copied ? t("projects.repoSshCopied") : t("projects.repoSshCopy")}
            </Button>
          )}
          <Button size="sm" onClick={() => create()}>
            {publicKey ? t("settings.sshKeyNew") : t("settings.sshKeyCreate")}
          </Button>
          {publicKey && (
            <Button size="sm" variant="ghost" onClick={remove}>
              {t("settings.sshKeyRemove")}
            </Button>
          )}
        </div>
        <details className="text-fg-3">
          <summary className="cursor-pointer">{t("settings.sshKeyOwn")}</summary>
          <div className="mt-2 space-y-2">
            <Textarea
              rows={3}
              className="font-mono"
              autoComplete="off"
              spellCheck={false}
              aria-label={t("settings.sshKeyOwn")}
              placeholder={t("projects.repoSshOwnPlaceholder")}
              value={own}
              onChange={(e) => setOwn(e.target.value)}
            />
            <Button size="sm" disabled={!own.trim()} onClick={() => create(own)}>
              {t("settings.sshKeyOwnSave")}
            </Button>
          </div>
        </details>
        <ErrorText error={error} />
      </div>
    </Field>
  );
}

function Section({ title, description, children }: { title: string; description: string; children: ReactNode }) {
  return (
    <section className="space-y-3">
      <div>
        <h3 className="font-semibold">{title}</h3>
        <p className="text-fg-3">{description}</p>
      </div>
      {children}
    </section>
  );
}
