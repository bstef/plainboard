import clsx from "clsx";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useLimits } from "../../api/queries";
import type { AccountLimits, LimitForecast, LimitWindow } from "../../api/types";
import { timeAgo } from "../../lib/format";

const tone = (used: number) => (used >= 0.9 ? "red" : used >= 0.75 ? "amber" : "indigo");
const stroke = { red: "text-you", amber: "text-fg", indigo: "text-fg-2" };
const bar = { red: "bg-you", amber: "bg-fg", indigo: "bg-fg-2" };
const percent = (used: number) => Math.round(used * 100);
const fromUnix = (seconds: number) => new Date(seconds * 1000).toISOString();

/** Plan limits (5-hour, weekly) as one small ring per account, filled to the fullest window; click
 * for the windows, resets and forecast. Sits under every composer, so usage is always in view. */
export function LimitsRing() {
  const { data } = useLimits();
  if (!data) return null;
  const known = data.accounts.filter((a) => a.windows.length);
  if (!known.length) return <Ring available={data.accounts.some((a) => a.available)} />;
  return (
    <>
      {known.map((account) => (
        <Ring key={account.account_id} account={account} />
      ))}
    </>
  );
}

/** Every account's limits written out, for where there is room (the chat's context column). */
export function LimitsList() {
  const { t } = useTranslation();
  const { data } = useLimits();
  if (!data) return null;
  const known = data.accounts.filter((a) => a.windows.length);
  if (!known.length) return <p className="px-2.5 py-2 text-fg-3">{t(data.accounts.some((a) => a.available) ? "chat.limits.unknown" : "chat.limits.noPlan")}</p>;
  return (
    <>
      {known.map((account) => (
        <div key={account.account_id} className="space-y-3 border-b border-line px-2.5 py-2.5">
          {known.length > 1 && <p className="font-semibold">{account.label}</p>}
          {account.windows.map((window) => (
            <LimitRow key={window.name} window={window} forecast={account.forecasts.find((f) => f.name === window.name)} />
          ))}
          {account.updated_at && <p className="text-fg-3">{t("chat.limits.updated", { when: timeAgo(fromUnix(account.updated_at)) })}</p>}
        </div>
      ))}
    </>
  );
}

function Ring({ account, available = true }: { account?: AccountLimits; available?: boolean }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => !ref.current?.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  const windows = account?.windows ?? [];
  const forecasts = account?.forecasts ?? [];
  const highest = Math.max(0, ...windows.map((w) => w.utilization));
  const slowDown = forecasts.some((f) => f.status === "over");
  const ringTone = tone(highest) === "indigo" && slowDown ? "amber" : tone(highest);
  const ringLabel = t("chat.limits.ring", { percent: percent(highest) });
  const label = (account ? `${account.label}: ${ringLabel}` : ringLabel) + (slowDown ? ` · ${t("chat.limits.slowDown")}` : "");

  return (
    <div ref={ref} className="relative shrink-0">
      <button type="button" onClick={() => setOpen(!open)} aria-label={label} aria-expanded={open} title={label} className="flex h-6 items-center gap-1.5 rounded-sm px-1 text-fg-3 hover:bg-sunk hover:text-fg touch:h-9">
        <svg viewBox="0 0 20 20" className="size-4 shrink-0 -rotate-90" aria-hidden>
          <circle cx="10" cy="10" r="8" fill="none" stroke="currentColor" strokeWidth="3" className="text-line-2" />
          <circle cx="10" cy="10" r="8" fill="none" stroke="currentColor" strokeWidth="3" pathLength={100} strokeDasharray={`${Math.min(percent(highest), 100)} 100`} className={stroke[ringTone]} />
        </svg>
        {account && <span className="max-w-24 truncate">{account.label}</span>}
      </button>
      {open && (
        <div className="absolute right-0 bottom-full z-20 mb-2 w-64 max-w-[calc(100vw-1.5rem)] space-y-3 rounded-sm bg-bg p-3 shadow-lg ring-1 ring-line-2">
          {account && <p className="font-semibold">{account.label}</p>}
          {windows.map((window) => (
            <LimitRow key={window.name} window={window} forecast={forecasts.find((f) => f.name === window.name)} />
          ))}
          {!windows.length && <p className="text-fg-3">{t(available ? "chat.limits.unknown" : "chat.limits.noPlan")}</p>}
          {account?.updated_at && <p className="text-fg-3">{t("chat.limits.updated", { when: timeAgo(fromUnix(account.updated_at)) })}</p>}
        </div>
      )}
    </div>
  );
}

const forecastTone = {
  ok: "text-ok",
  tight: "text-fg",
  over: "text-you-fg",
};

function Forecast({ forecast }: { forecast: LimitForecast }) {
  const { t } = useTranslation();
  const headline =
    forecast.status === "over"
      ? forecast.runs_out_at
        ? t("chat.limits.forecast.over", { percent: percent(forecast.projected), when: timeAgo(fromUnix(forecast.runs_out_at)) })
        : t("chat.limits.forecast.reached")
      : t(`chat.limits.forecast.${forecast.status}`, { percent: percent(forecast.projected) });
  return (
    <div className="mt-1.5 space-y-0.5">
      <p className={clsx("font-medium", forecastTone[forecast.status])}>{headline}</p>
      <p className="text-fg-3">{t("chat.limits.forecast.pace", { daily: percent(forecast.daily), budget: percent(forecast.budget) })}</p>
      <p className="text-fg-3">{forecast.basis_days ? t("chat.limits.forecast.basisDays", { count: forecast.basis_days }) : t("chat.limits.forecast.basisWeek")}</p>
    </div>
  );
}

function LimitRow({ window, forecast }: { window: LimitWindow; forecast?: LimitForecast }) {
  const { t } = useTranslation();
  return (
    <div>
      <div className="flex justify-between gap-2 font-medium">
        <span>{t(`chat.limits.${window.name}`, { defaultValue: window.name })}</span>
        <span>{percent(window.utilization)}%</span>
      </div>
      <div className="mt-1 h-1.5 bg-line">
        <div className={clsx("h-full", bar[tone(window.utilization)])} style={{ width: `${Math.min(percent(window.utilization), 100)}%` }} />
      </div>
      {window.resets_at && <p className="mt-1 text-fg-3">{t("chat.limits.resets", { when: timeAgo(fromUnix(window.resets_at)) })}</p>}
      {forecast && <Forecast forecast={forecast} />}
    </div>
  );
}
