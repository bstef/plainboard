import { useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { api, errorMessage } from "../../api/client";
import { Button, Input } from "../../components/ui";

export function Login() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.post("/auth/login", { password });
      await qc.invalidateQueries();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex h-full items-center justify-center p-6">
      <form onSubmit={submit} className="w-full max-w-xs space-y-4">
        <div className="flex items-center gap-2 font-semibold">
          <Logo /> PlainBoard
        </div>
        <Input type="password" autoFocus placeholder={t("auth.password")} value={password} onChange={(e) => setPassword(e.target.value)} />
        {error && <p className="text-you-fg">{error}</p>}
        <Button type="submit" variant="primary" className="w-full" loading={busy}>
          {t("auth.signIn")}
        </Button>
      </form>
    </div>
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
