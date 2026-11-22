import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, KeyRound, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { qk, useAccounts, useMe, useUsers } from "../../api/queries";
import type { RunnerInfo, User } from "../../api/types";
import { Badge, Button, ErrorText, Field, FieldRow, IconButton, Input, Toggle } from "../../components/ui";

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
  const [open, setOpen] = useState(false);
  const patch = (body: Record<string, unknown>) => onChange(() => api.patch(`/users/${user.id}`, body));
  const limited = !self && !user.is_admin; // an admin may use everything

  return (
    <li className="space-y-2 px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        {limited ? (
          <button type="button" className="flex min-w-0 flex-1 items-center gap-1 text-left" aria-expanded={open} onClick={() => setOpen(!open)}>
            {open ? <ChevronDown className="size-4 shrink-0 text-fg-3" /> : <ChevronRight className="size-4 shrink-0 text-fg-3" />}
            <span className="min-w-0">
              <span className="font-medium">{user.name}</span> <span className="text-fg-3">{user.username}</span>
            </span>
          </button>
        ) : (
          <div className="min-w-0 flex-1">
            <span className="font-medium">{user.name}</span> <span className="text-fg-3">{user.username}</span>
            {self && <span className="text-fg-3"> · {t("users.you")}</span>}
          </div>
        )}
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
      {limited && open && <UserLimits user={user} patch={patch} />}
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

/** What the projects this person owns may use. Saved right away; empty or "all": no limit. */
function UserLimits({ user, patch }: { user: User; patch: (body: Record<string, unknown>) => Promise<boolean> }) {
  const { t } = useTranslation();
  const accounts = useAccounts().data ?? [];
  const runners = useQuery({ queryKey: ["runners"], queryFn: () => api.get<RunnerInfo[]>("/runners") }).data ?? [];

  return (
    <div className="space-y-3 border-l-2 border-line pl-3" data-part="user-limits">
      <p className="text-fg-3">{t("users.limitsHint")}</p>
      <FieldRow>
        {user.may_create_projects && (
          <Field label={t("users.maxProjects")} hint={t("users.maxProjectsHint")}>
            <NumberInput value={user.max_projects} max={1000} onSave={(v) => patch({ max_projects: v })} />
          </Field>
        )}
        <Field label={t("users.maxParallel")} hint={t("users.maxParallelHint")}>
          <NumberInput value={user.max_parallel} max={100} onSave={(v) => patch({ max_parallel: v })} />
        </Field>
      </FieldRow>
      <Choice
        label={t("users.subscriptions")}
        all={t("users.allSubscriptions")}
        items={accounts.map((a) => ({ id: a.id, label: a.label }))}
        value={user.account_ids}
        onChange={(ids) => patch({ account_ids: ids })}
      />
      <Choice
        label={t("users.runners")}
        all={t("users.allRunners")}
        items={[{ id: 0, label: t("projectRunner.builtin") }, ...runners.map((r) => ({ id: r.id, label: r.name }))]}
        value={user.runner_ids}
        onChange={(ids) => patch({ runner_ids: ids })}
      />
    </div>
  );
}

function NumberInput({ value, max, onSave }: { value: number | null; max: number; onSave: (v: number | null) => void }) {
  const [text, setText] = useState(value === null ? "" : String(value));
  const save = () => {
    const next = text.trim() === "" ? null : Math.min(max, Math.max(0, Math.floor(Number(text)) || 0));
    setText(next === null ? "" : String(next));
    if (next !== value) onSave(next);
  };
  return <Input type="number" inputMode="numeric" min={0} max={max} value={text} onChange={(e) => setText(e.target.value)} onBlur={save} onKeyDown={(e) => e.key === "Enter" && save()} />;
}

/** All (null), or only the ticked ones. */
function Choice({ label, all, items, value, onChange }: { label: string; all: string; items: { id: number; label: string }[]; value: number[] | null; onChange: (ids: number[] | null) => void }) {
  return (
    <div className="space-y-1">
      <span className="block font-medium text-fg-2">{label}</span>
      <Toggle checked={value === null} onChange={(v) => onChange(v ? null : items.map((i) => i.id))} label={all} />
      {value !== null && (
        <ul className="space-y-0.5">
          {items.map((item) => (
            <li key={item.id}>
              <label className="flex cursor-pointer items-center gap-2 rounded-sm px-1.5 py-1 hover:bg-sunk touch:py-2">
                <input
                  type="checkbox"
                  checked={value.includes(item.id)}
                  onChange={() => onChange(value.includes(item.id) ? value.filter((id) => id !== item.id) : [...value, item.id])}
                />
                <span className="min-w-0 truncate">{item.label}</span>
              </label>
            </li>
          ))}
        </ul>
      )}
    </div>
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
