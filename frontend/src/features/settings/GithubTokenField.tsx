import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { errorMessage } from "../../api/client";
import { checks } from "../../api/queries";
import type { CheckResult } from "../../api/types";
import { Button, Input } from "../../components/ui";
import { CheckBadge } from "./SettingsDialog";

/** A GitHub token with a button that asks GitHub about it. What is typed is tested before it is
 * saved; with an empty field, the token that is stored (for a project: with its repositories). */
export function GithubTokenField({
  label,
  value,
  onChange,
  projectId,
  children,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  projectId?: number;
  children?: ReactNode;
}) {
  const { t } = useTranslation();
  const [check, setCheck] = useState<CheckResult | null>(null);
  const [busy, setBusy] = useState(false);
  const test = async () => {
    setBusy(true);
    setCheck(null);
    try {
      setCheck(value || !projectId ? await checks.github(value || undefined) : await checks.projectGithub(projectId));
    } catch (err) {
      setCheck({ ok: false, detail: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="space-y-1">
      <div className="flex gap-2">
        <Input
          type="password"
          autoComplete="off"
          aria-label={label}
          value={value}
          onChange={(e) => {
            setCheck(null);
            onChange(e.target.value);
          }}
        />
        {children}
        <Button loading={busy} onClick={test}>
          {t("settings.testToken")}
        </Button>
      </div>
      <CheckBadge result={check} />
    </div>
  );
}
