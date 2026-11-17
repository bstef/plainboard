import { useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { api, errorMessage } from "../../api/client";
import type { AuthStatus } from "../../api/queries";
import { Button, Field, Input } from "../../components/ui";

/** The page for someone who is not signed in: a narrow column in the middle. */
export function AuthPage({ title, intro, children }: { title: string; intro?: ReactNode; children: ReactNode }) {
  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <div className="w-full max-w-sm space-y-5">
        <div className="flex items-center gap-2 font-extrabold">
          <Logo /> PlainBoard
        </div>
        <div className="space-y-1">
          <h1 className="font-extrabold">{title}</h1>
          {intro && <p className="text-fg-3">{intro}</p>}
        </div>
        {children}
      </div>
    </div>
  );
}

export function Login() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.post("/auth/login", { username, password });
      await qc.invalidateQueries();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthPage title={t("auth.signIn")}>
      <form onSubmit={submit} className="space-y-4">
        <Field label={t("auth.username")}>
          <Input autoFocus autoComplete="username" autoCapitalize="none" spellCheck={false} value={username} onChange={(e) => setUsername(e.target.value)} />
        </Field>
        <Field label={t("auth.password")}>
          <Input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        {error && <p className="text-you-fg">{error}</p>}
        <Button type="submit" variant="primary" className="w-full" loading={busy} disabled={!username.trim() || !password}>
          {t("auth.signIn")}
        </Button>
      </form>
    </AuthPage>
  );
}

/** A new installation: whoever opens it first creates the admin account. */
export function CreateAdmin({ status }: { status: AuthStatus }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [form, setForm] = useState({ name: "", username: "", password: "", repeat: "", setup_password: "" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (patch: Partial<typeof form>) => setForm((f) => ({ ...f, ...patch }));
  const mismatch = form.repeat !== "" && form.repeat !== form.password;
  const ready = form.username.trim().length >= 2 && form.password.length >= 8 && form.repeat === form.password && (!status.setup_password_required || form.setup_password !== "");

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.post("/auth/setup", { name: form.name, username: form.username, password: form.password, setup_password: form.setup_password || null });
      await qc.invalidateQueries();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthPage title={t("auth.setupTitle")} intro={t("auth.setupIntro")}>
      <form onSubmit={submit} className="space-y-4">
        {status.setup_password_required && (
          <Field label={t("auth.setupPassword")} hint={t("auth.setupPasswordHint")}>
            <Input type="password" autoFocus autoComplete="off" value={form.setup_password} onChange={(e) => set({ setup_password: e.target.value })} />
          </Field>
        )}
        <Field label={t("auth.name")} hint={t("auth.nameHint")}>
          <Input autoFocus={!status.setup_password_required} autoComplete="name" value={form.name} onChange={(e) => set({ name: e.target.value })} />
        </Field>
        <Field label={t("auth.username")} hint={t("auth.usernameHint")}>
          <Input autoComplete="username" autoCapitalize="none" spellCheck={false} value={form.username} onChange={(e) => set({ username: e.target.value })} />
        </Field>
        <Field label={t("auth.password")} hint={t("auth.passwordHint")}>
          <Input type="password" autoComplete="new-password" value={form.password} onChange={(e) => set({ password: e.target.value })} />
        </Field>
        <Field label={t("auth.passwordRepeat")} hint={mismatch ? <span className="text-you-fg">{t("auth.passwordMismatch")}</span> : undefined}>
          <Input type="password" autoComplete="new-password" value={form.repeat} onChange={(e) => set({ repeat: e.target.value })} />
        </Field>
        {error && <p className="text-you-fg">{error}</p>}
        <Button type="submit" variant="primary" className="w-full" loading={busy} disabled={!ready}>
          {t("auth.setupCreate")}
        </Button>
      </form>
    </AuthPage>
  );
}

/** Three bars of falling height: columns of a board, as a mark in the text colour. */
export function Logo({ className = "size-4" }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" fill="currentColor" className={className} aria-hidden>
      <rect x="1.5" y="2" width="3.4" height="12" />
      <rect x="6.3" y="2" width="3.4" height="8.4" />
      <rect x="11.1" y="2" width="3.4" height="5.2" />
    </svg>
  );
}
