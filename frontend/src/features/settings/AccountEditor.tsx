import { useQueryClient } from "@tanstack/react-query";
import { LogOut } from "lucide-react";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { qk, useMe } from "../../api/queries";
import { Avatar, squarePicture } from "../../components/Avatar";
import { Button, ErrorText, Field, Input } from "../../components/ui";

/** One's own account: picture, name, password, signing out. Saved right away. */
export function AccountEditor() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const me = useMe();
  const [name, setName] = useState(me?.name ?? "");
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [saved, setSaved] = useState(false);
  const picker = useRef<HTMLInputElement>(null);
  if (!me) return null;

  /** The picture is saved the moment it is chosen (or removed). */
  const picture = async (file: File | null) => {
    setError(null);
    try {
      if (file) {
        const body = new FormData();
        const square = await squarePicture(file).catch(() => Promise.reject(new Error(t("account.pictureUnreadable"))));
        body.append("file", square, "picture");
        await api.put("/auth/me/avatar", body);
      } else {
        await api.del("/auth/me/avatar");
      }
      await Promise.all([qc.invalidateQueries({ queryKey: qk.auth }), qc.invalidateQueries({ queryKey: qk.directory })]);
    } catch (err) {
      setError(err);
    }
  };

  const run = async (body: Record<string, unknown>) => {
    setError(null);
    setSaved(false);
    try {
      await api.patch("/auth/me", body);
      await qc.invalidateQueries({ queryKey: qk.auth });
      setCurrent("");
      setNext("");
      setSaved(true);
    } catch (err) {
      setError(err);
    }
  };
  const signOut = async () => {
    await api.post("/auth/logout");
    window.location.reload();  // nothing of this user stays in memory
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <Avatar name={me.name} person={me} className="size-14 text-xl" />
        <div className="min-w-0">
          <div className="flex flex-wrap gap-2">
            <Button size="sm" onClick={() => picker.current?.click()}>
              {t(me.avatar_version ? "account.changePicture" : "account.addPicture")}
            </Button>
            {me.avatar_version > 0 && (
              <Button size="sm" variant="ghost" onClick={() => picture(null)}>
                {t("account.removePicture")}
              </Button>
            )}
          </div>
          <p className="mt-1 text-fg-3">{t("account.pictureHint")}</p>
        </div>
        <input
          ref={picker}
          type="file"
          accept="image/*"
          hidden
          onChange={(e) => {
            const file = e.target.files?.[0];
            e.target.value = "";
            if (file) void picture(file);
          }}
        />
      </div>
      <div className="flex items-end gap-2">
        <Field label={t("auth.name")} hint={t("account.signedInAs", { username: me.username })} className="min-w-0 flex-1">
          <Input value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={t("account.currentPassword")}>
          <Input type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} />
        </Field>
        <Field label={t("account.newPassword")} hint={t("auth.passwordHint")}>
          <Input type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} />
        </Field>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          variant="primary"
          disabled={name.trim() === me.name && !(current && next.length >= 8)}
          onClick={() => run({ ...(name.trim() !== me.name ? { name } : {}), ...(current && next.length >= 8 ? { current_password: current, new_password: next } : {}) })}
        >
          {t("account.save")}
        </Button>
        {saved && <span className="text-ok">{t("account.saved")}</span>}
        <div className="flex-1" />
        <Button size="sm" icon={<LogOut className="size-3.5" />} onClick={signOut}>
          {t("account.signOut")}
        </Button>
      </div>
      <ErrorText error={error} />
    </div>
  );
}
