import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { qk, useAccounts, useMe, useMeta, useRepositories, useSettings } from "../../api/queries";
import type { Agent, Project } from "../../api/types";
import { Field, Input, Select, Textarea, Toggle } from "../../components/ui";
import { MembersEditor } from "../projects/MembersEditor";
import { ImportProject } from "../projects/MoveProject";
import { RepositoriesEditor, toBody, toDraft } from "../projects/RepositoriesEditor";
import { SecretsEditor } from "../settings/SecretsEditor";
import { ModelSelect } from "../team/AgentEditor";
import { usable } from "./SetupWizard";
import { Advice, Wizard, type WizardStep } from "./Wizard";

/** A new project, step by step. It exists after the first step, so everything after it is saved
 * where it belongs right away, and closing early loses nothing. */
export function ProjectWizard({ onClose, onCreated }: { onClose: () => void; onCreated: (p: Project) => void }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const me = useMe();
  const meta = useMeta();
  const settings = useSettings();
  const accounts = useAccounts();
  const defaults = settings.data?.values ?? {};
  const [project, setProject] = useState<Project | null>(null);
  const [manager, setManager] = useState<Agent | null>(null);
  const [basics, setBasics] = useState({ name: "", description: "" });
  const [code, setCode] = useState({ workspace_path: "", github_token: "" });
  const [team, setTeam] = useState<{ model: string; effort: string; account_id: number | null; parallel_agents: number | null; auto_wake_manager: boolean; continue_with_backlog: boolean }>({
    model: "",
    effort: "",
    account_id: null,
    parallel_agents: null,
    auto_wake_manager: true,
    continue_with_backlog: true,
  });
  const repos = useRepositories(project?.id ?? 0);
  const refreshRepos = () => project && qc.invalidateQueries({ queryKey: qk.repos(project.id) });
  const provider = (meta.data?.providers ?? []).find((p) => p.name === (manager?.provider ?? defaults.default_provider ?? "claude_code"));
  const usableAccounts = (accounts.data ?? []).filter((a) => usable(a) && a.provider === provider?.name);

  const saveBasics = async () => {
    // A project made here starts with the two tickets that set it up.
    const saved = project ? await api.patch<Project>(`/projects/${project.id}`, basics) : await api.post<Project>("/projects", { ...basics, first_steps: true });
    setProject(saved);
    await qc.invalidateQueries({ queryKey: qk.projects });
    if (!manager) {
      const found = (await api.get<Agent[]>(`/projects/${saved.id}/agents`)).find((a) => a.is_manager) ?? null;
      setManager(found);
      if (found) setTeam((f) => ({ ...f, model: found.model, effort: found.effort ?? "" }));
    }
  };
  const saveCode = async () => {
    if (!project) return;
    setProject(await api.patch<Project>(`/projects/${project.id}`, { workspace_path: code.workspace_path || null, ...(code.github_token ? { github_token: code.github_token } : {}) }));
    setCode((c) => ({ ...c, github_token: "" }));
  };
  const saveTeam = async () => {
    if (!project) return;
    if (manager) setManager(await api.patch<Agent>(`/agents/${manager.id}`, { model: team.model, effort: team.effort || null }));
    setProject(await api.patch<Project>(`/projects/${project.id}`, { account_id: team.account_id, parallel_agents: team.parallel_agents, auto_wake_manager: team.auto_wake_manager, continue_with_backlog: team.continue_with_backlog }));
    await qc.invalidateQueries({ queryKey: qk.agents(project.id) });
  };
  const finish = async () => {
    await qc.invalidateQueries({ queryKey: qk.projects });
    if (project) onCreated(project);
    onClose();
  };

  const steps: WizardStep[] = [
    {
      id: "basics",
      label: t("newProject.basics.label"),
      title: t("newProject.basics.title"),
      intro: t("newProject.basics.intro"),
      ready: basics.name.trim() !== "",
      missing: t("newProject.basics.missing"),
      onNext: saveBasics,
      content: (
        <div className="space-y-4">
          <Field label={t("projects.name")}>
            <Input autoFocus value={basics.name} onChange={(e) => setBasics({ ...basics, name: e.target.value })} />
          </Field>
          <Field label={t("projects.description")} hint={t("newProject.basics.descriptionHint")}>
            <Textarea rows={6} value={basics.description} onChange={(e) => setBasics({ ...basics, description: e.target.value })} />
          </Field>
          {/* The other way to a project: bring one along from another PlainBoard. Only before this one exists. */}
          {!project && (
            <details className="border-t border-line pt-4">
              <summary className="cursor-pointer font-medium">{t("move.importTitle")}</summary>
              <div className="pt-3">
                <ImportProject
                  onOpen={(imported) => {
                    onCreated(imported);
                    onClose();
                  }}
                />
              </div>
            </details>
          )}
        </div>
      ),
    },
    {
      id: "code",
      label: t("newProject.code.label"),
      title: t("newProject.code.title"),
      intro: t("newProject.code.intro"),
      onNext: saveCode,
      content: project && (
        <div className="space-y-4">
          <Advice>{t("newProject.code.advice")}</Advice>
          <Field group label={t("projects.repositories")} hint={t("projects.repositoriesHint")}>
            <RepositoriesEditor
              repos={(repos.data ?? []).map(toDraft)}
              projectId={project.id}
              onAdd={async (d) => {
                await api.post(`/projects/${project.id}/repositories`, toBody(d));
                refreshRepos();
              }}
              onSave={async (d) => {
                await api.patch(`/repositories/${d.key}`, { ...toBody(d), new_ssh_key: d.new_ssh_key });
                refreshRepos();
              }}
              onRemove={async (d) => {
                await api.del(`/repositories/${d.key}`);
                refreshRepos();
              }}
            />
          </Field>
          <Field label={t("projects.githubToken")} hint={project.has_github_token ? t("projects.githubTokenSet") : t("projects.githubTokenHint")}>
            <Input type="password" autoComplete="off" value={code.github_token} onChange={(e) => setCode({ ...code, github_token: e.target.value })} />
          </Field>
          <Field label={t("projects.localFolder")} hint={t("projects.localFolderHint")}>
            <Input placeholder="my-app" value={code.workspace_path} onChange={(e) => setCode({ ...code, workspace_path: e.target.value })} />
          </Field>
        </div>
      ),
    },
    {
      id: "team",
      label: t("newProject.team.label"),
      title: t("newProject.team.title"),
      intro: t("newProject.team.intro"),
      ready: !!team.model,
      missing: t("setup.models.missing"),
      onNext: saveTeam,
      content: (
        <div className="space-y-4">
          <Advice>{t("newProject.team.advice")}</Advice>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label={t("settings.managerModel")} hint={t("setup.models.managerHint")}>
              <ModelSelect provider={provider} value={team.model} onChange={(v) => setTeam({ ...team, model: v })} />
            </Field>
            <Field label={t("team.effort")} hint={t("setup.models.effortHint")}>
              <Select value={team.effort} onChange={(e) => setTeam({ ...team, effort: e.target.value })}>
                <option value="">{t("team.effortDefault")}</option>
                {(provider?.efforts ?? []).map((e) => (
                  <option key={e} value={e}>
                    {e}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
          <Field label={t("newProject.team.subscription")} hint={t("newProject.team.subscriptionHint")}>
            <Select value={team.account_id ?? ""} onChange={(e) => setTeam({ ...team, account_id: e.target.value ? Number(e.target.value) : null })}>
              <option value="">{t("newProject.team.subscriptionAuto")}</option>
              {usableAccounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.label}
                </option>
              ))}
            </Select>
          </Field>
          <Field label={t("projects.parallel")} hint={t("projects.parallelHint", { count: Number(defaults.max_parallel_runs ?? 3) })}>
            <Input
              type="number"
              min={0}
              max={20}
              className="w-28"
              value={team.parallel_agents ?? ""}
              placeholder={String(defaults.max_parallel_runs ?? 3)}
              onChange={(e) => setTeam({ ...team, parallel_agents: e.target.value === "" ? null : Math.max(0, Math.min(20, Math.round(Number(e.target.value) || 0))) })}
            />
          </Field>
          <div className="space-y-1">
            <Toggle checked={team.auto_wake_manager} onChange={(v) => setTeam({ ...team, auto_wake_manager: v })} label={t("projects.autoWake")} />
            <p className="text-fg-3">{t("projects.autoWakeHint")}</p>
            <Toggle checked={team.continue_with_backlog} onChange={(v) => setTeam({ ...team, continue_with_backlog: v })} label={t("projects.backlogNext")} />
            <p className="text-fg-3">{t("projects.backlogNextHint")}</p>
          </div>
        </div>
      ),
    },
    {
      id: "secrets",
      label: t("newProject.secrets.label"),
      title: t("newProject.secrets.title"),
      intro: t("newProject.secrets.intro"),
      content: project && <SecretsEditor projectId={project.id} />,
    },
    {
      id: "members",
      label: t("newProject.members.label"),
      title: t("newProject.members.title"),
      intro: t(me?.is_admin ? "newProject.members.introAdmin" : "newProject.members.intro"),
      content: project && <MembersEditor projectId={project.id} />,
    },
  ];

  return <Wizard heading={project ? project.name : t("projects.new")} steps={steps} doneLabel={t("newProject.finish")} onDone={finish} onClose={finish} />;
}
