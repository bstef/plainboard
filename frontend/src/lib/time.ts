import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

/** The current time, refreshed on an interval, for durations that keep counting on screen. */
export function useNow(intervalMs = 30_000): number {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}

/** Formats how long ago a timestamp was, as one short unit ("12m", "2h"). */
export function useSince(intervalMs = 30_000) {
  const { t } = useTranslation();
  const now = useNow(intervalMs);
  return useCallback(
    (iso: string | null | undefined): string => {
      if (!iso) return "";
      const minutes = Math.floor((now - Date.parse(iso)) / 60_000);
      if (!Number.isFinite(minutes)) return "";
      if (minutes < 1) return t("time.now");
      if (minutes < 60) return t("time.minutes", { n: minutes });
      const hours = Math.floor(minutes / 60);
      return hours < 24 ? t("time.hours", { n: hours }) : t("time.days", { n: Math.floor(hours / 24) });
    },
    [now, t],
  );
}
