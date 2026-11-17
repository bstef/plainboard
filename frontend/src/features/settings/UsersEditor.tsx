import { useQueryClient } from "@tanstack/react-query";
import { KeyRound, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { qk, useMe, useUsers } from "../../api/queries";
import type { User } from "../../api/types";
import { Badge, Button, ErrorText, Field, IconButton, Input, Toggle } from "../../components/ui";

/** The people who can sign in. Saved right away. Admins only. */
export function UsersEditor() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const me = useMe();
  const users = useUsers();
  const [error, setError] = useState<unknown>(null);
  const [adding, setAdding] = useState(false);

  const run = async (action: () => Promise<unknown>) => {
    setError(null);
    try {
      await action();
      await Promise.all([qc.invalidateQueries({ queryKey: qk.users }), qc.invalidateQueries({ queryKey: qk.auth })]);
      return true;
    } catch (err) {
      setError(err);
      return false;
    }
  };

  return (
    <div className="space-y-3">
      <ul className="divide-y divide-line rounded-sm ring-1 ring-line">
        {(users.data ?? []).map((user) => (
          <UserRow key={user.id} user={user} self={user.id === me?.id} onChange={run} />
        ))}
      </ul>
      {adding ? (
        <NewUser onCancel={() => setAdding(false)} onCreate={async (body) => (await run(() => api.post("/users", body))) && setAdding(false)} />
      ) : (
        <Button size="sm" icon={<Plus className="size-3.5" />} onClick={() => setAdding(true)}>
          {t("users.add")}
        </Button>
      )}
      <ErrorText error={error} />
    </div>
  );
}

function UserRow({ user, self, onChange }: { user: User; self: boolean; onChange: (action: () => Promise<unknown>) => Promise<boolean> }) {
  const { t } = useTranslation();
  const [password, setPassword] = useState<string | null>(null);
  const patch = (body: Record<string, unknown>) => onChange(() => api.patch(`/users/${user.id}`, body));

  return (
    <li className="space-y-2 px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <div className="min-w-0 flex-1">
          <span className="font-medium">{user.name}</span> <span className="text-fg-3">{user.username}</span>
          {self && <span className="text-fg-3"> · {t("users.you")}</span>}
        </div>
        {user.is_admin && <Badge>{t("users.admin")}</Badge>}
        {!user.enabled && <Badge tone="amber">{t("users.disabled")}</Badge>}
        <IconButton label={t("users.newPassword")} onClick={() => setPassword(password === null ? "" : null)}>
          <KeyRound className="size-4" />
        </IconButton>
        {!self && (
          <IconButton label={t("users.delete")} onClick={() => confirm(t("users.confirmDelete", { name: user.name })) && onChange(() => api.del(`/users/${user.id}`))}>
            <Trash2 className="size-4" />
          </IconButton>
        )}
      </div>
      {!self && (
        <div className="flex flex-wrap gap-x-5 gap-y-2">
          <Toggle checked={user.is_admin} onChange={(v) => patch({ is_admin: v })} label={t("users.admin")} />
          {!user.is_admin && <Toggle checked={user.may_create_projects} onChange={(v) => patch({ may_create_projects: v })} label={t("users.mayCreate")} />}
          <Toggle checked={user.enabled} onChange={(v) => patch({ enabled: v })} label={t("users.enabled")} />
        </div>
      )}
      {password !== null && (
        <form
          className="flex gap-2"
          onSubmit={async (e) => {
            e.preventDefault();
            if (await patch({ password })) setPassword(null);
          }}
        >
          <Input type="password" autoFocus autoComplete="new-password" placeholder={t("users.newPasswordPlaceholder")} value={password} onChange={(e) => setPassword(e.target.value)} />
          <Button type="submit" variant="primary" disabled={password.length < 8}>
            {t("common.save")}
          </Button>
        </form>
      )}
    </li>
  );
}

function NewUser({ onCreate, onCancel }: { onCreate: (body: Record<string, unknown>) => void; onCancel: () => void }) {
  const { t } = useTranslation();
  const [form, setForm] = useState({ name: "", username: "", password: "", is_admin: false, may_create_projects: false });
  const set = (patch: Partial<typeof form>) => setForm((f) => ({ ...f, ...patch }));

  return (
    <form
      className="space-y-3 rounded-sm p-3 ring-1 ring-line"
      onSubmit={(e) => {
        e.preventDefault();
        onCreate(form);
      }}
    >
      <div className="grid gap-3 sm:grid-cols-3">
        <Field label={t("auth.name")}>
          <Input autoFocus value={form.name} onChange={(e) => set({ name: e.target.value })} />
        </Field>
        <Field label={t("auth.username")}>
          <Input autoCapitalize="none" spellCheck={false} autoComplete="off" value={form.username} onChange={(e) => set({ username: e.target.value })} />
        </Field>
        <Field label={t("auth.password")}>
          <Input type="password" autoComplete="new-password" value={form.password} onChange={(e) => set({ password: e.target.value })} />
        </Field>
      </div>
      <p className="text-fg-3">{t("users.addHint")}</p>
      <div className="flex flex-wrap gap-x-5 gap-y-2">
        <Toggle checked={form.is_admin} onChange={(v) => set({ is_admin: v })} label={t("users.admin")} />
        {!form.is_admin && <Toggle checked={form.may_create_projects} onChange={(v) => set({ may_create_projects: v })} label={t("users.mayCreate")} />}
      </div>
      <div className="flex justify-end gap-2">
        <Button onClick={onCancel}>{t("common.cancel")}</Button>
        <Button type="submit" variant="primary" disabled={form.username.trim().length < 2 || form.password.length < 8}>
          {t("users.create")}
        </Button>
      </div>
    </form>
  );
}
