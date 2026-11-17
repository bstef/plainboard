import { Check, Pause, PauseCircle, Play } from "lucide-react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { qk, useApiMutation, useMe, usePause } from "../../api/queries";
import type { PauseStatus } from "../../api/types";
import { Button, ErrorText, Spinner } from "../../components/ui";

/** Pausing and resuming everything, for whoever may: both answer with the new state. */
function usePauseSwitch() {
  return useApiMutation(
    (on: boolean) => (on ? api.post<PauseStatus>("/pause") : api.del<PauseStatus>("/pause")),
    () => [qk.pause, ["agents"]],
  );
}

const clock = (iso: string | null) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "");

/** Bring every project to rest before an update or a restart, see when everything is at rest, and resume. */
export function PauseEditor() {
  const { t } = useTranslation();
  const pause = usePause().data;
  const turn = usePauseSwitch();
  if (!pause) return null;

  if (!pause.paused)
    return (
      <div className="space-y-4">
        <ol className="list-decimal space-y-1 pl-5 text-fg-2">
          <li>{t("pause.step1")}</li>
          <li>{t("pause.step2")}</li>
          <li>{t("pause.step3")}</li>
          <li>{t("pause.step4")}</li>
        </ol>
        <Button variant="primary" icon={<Pause className="size-3.5" />} loading={turn.isPending} onClick={() => turn.mutate(true)}>
          {t("pause.start")}
        </Button>
        <ErrorText error={turn.error} />
      </div>
    );

  return (
    <div className="space-y-4">
      <p>{t("pause.since", { by: pause.by || t("pause.someone"), time: clock(pause.since) })}</p>
      {pause.standing ? (
        <div role="status" className="flex items-start gap-2.5">
          <Check className="mt-0.5 size-4 shrink-0 text-ok" />
          <div>
            <p className="font-semibold text-ok">{t("pause.standing")}</p>
            <p className="text-fg-2">{t("pause.standingHint")}</p>
          </div>
        </div>
      ) : (
        <div role="status">
          <p className="flex items-center gap-2 font-semibold">
            <Spinner /> {t("pause.moving", { count: pause.moving.length })}
          </p>
          <ul className="mt-2">
            {pause.moving.map((item, i) => (
              <li key={i} className="flex flex-wrap items-baseline gap-x-2 border-b border-line py-2">
                <span className="font-medium">{[item.project, item.agent].filter(Boolean).join(" · ")}</span>
                <span className="text-fg-2">{t(`pause.what.${item.what}`)}</span>
                {item.detail && <code className="min-w-0 truncate text-fg-3">{item.detail}</code>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {pause.servers > 0 && <p className="text-fg-2">{t("pause.servers", { count: pause.servers })}</p>}
      <div className="space-y-2 border-t border-line pt-4">
        <Button variant={pause.standing ? "primary" : "secondary"} icon={<Play className="size-3.5" />} loading={turn.isPending} onClick={() => turn.mutate(false)}>
          {t("pause.resume")}
        </Button>
        <p className="text-fg-3">{t("pause.resumeHint")}</p>
        <ErrorText error={turn.error} />
      </div>
    </div>
  );
}

/** Everything is paused: said to everyone, on every screen. An admin goes on from here. */
export function PauseBanner({ onDetails }: { onDetails: () => void }) {
  const { t } = useTranslation();
  const pause = usePause().data;
  const admin = !!useMe()?.is_admin;
  const turn = usePauseSwitch();
  if (!pause?.paused) return null;
  const by = pause.by || t("pause.someone");
  return (
    <div role="status" className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b border-line bg-you-bg px-4 py-1.5 text-you-fg">
      <PauseCircle className="size-4 shrink-0" />
      <span className="min-w-0 flex-1 basis-48">{pause.standing ? t("pause.bannerStanding", { by }) : t("pause.bannerMoving", { by, count: pause.moving.length })}</span>
      {admin && (
        <span className="flex shrink-0 items-center gap-3">
          <button type="button" onClick={onDetails} className="underline underline-offset-[3px] hover:decoration-2">
            {t("pause.details")}
          </button>
          <button type="button" disabled={turn.isPending} onClick={() => turn.mutate(false)} className="font-semibold underline underline-offset-[3px] hover:decoration-2 disabled:opacity-60">
            {t("pause.resume")}
          </button>
        </span>
      )}
    </div>
  );
}
