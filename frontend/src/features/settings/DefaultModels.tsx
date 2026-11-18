import { useTranslation } from "react-i18next";
import type { ProviderInfo } from "../../api/types";
import { Field, Select } from "../../components/ui";
import { ModelSelect } from "../team/AgentEditor";

type Values = Record<string, any>;

const ROLES = [
  { model: "default_manager_model", effort: "default_manager_effort", label: "settings.managerModel", hint: "setup.models.managerHint" },
  { model: "default_worker_model", effort: "default_worker_effort", label: "settings.workerModel", hint: "setup.models.workerHint" },
  { model: "default_summary_model", effort: "default_summary_effort", label: "settings.summaryModel", hint: "setup.models.summaryHint" },
] as const;

/** What new agents get, for managers, members and summaries: the model, and beside it how long it thinks. */
export function DefaultModels({ provider, values, set }: { provider?: ProviderInfo; values: Values; set: (patch: Values) => void }) {
  const { t } = useTranslation();
  return (
    <>
      {ROLES.map((role) => {
        const effort: string = values[role.effort] ?? "";
        // A value the chosen provider does not list (it was set for another one) stays visible.
        const efforts = [...(provider?.efforts ?? []), ...(effort && !provider?.efforts.includes(effort) ? [effort] : [])];
        return (
          <Field key={role.model} group label={t(role.label)} hint={t(role.hint)}>
            <div className="flex items-start gap-2">
              <div className="min-w-0 flex-1">
                <ModelSelect provider={provider} value={values[role.model] ?? ""} onChange={(v) => set({ [role.model]: v })} />
              </div>
              <div className="w-32 shrink-0 sm:w-40">
                <Select aria-label={t("settings.defaultEffort")} value={effort} onChange={(e) => set({ [role.effort]: e.target.value || null })}>
                  <option value="">{t("settings.effortOwn")}</option>
                  {efforts.map((e) => (
                    <option key={e} value={e}>
                      {t("settings.effortOption", { effort: e })}
                    </option>
                  ))}
                </Select>
              </div>
            </div>
          </Field>
        );
      })}
      <p className="text-fg-3">{t("setup.models.effortHint")}</p>
    </>
  );
}
