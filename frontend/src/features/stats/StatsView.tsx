import clsx from "clsx";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useStats } from "../../api/queries";
import type { StatsDay } from "../../api/types";
import { AgentSquare, PageHead, Segmented, Spinner } from "../../components/ui";
import { useShell } from "../layout/shell-context";

const RANGES = [7, 30, 90];
const compact = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
const money = (value: number) => `$${value.toFixed(2)}`;
const count = (value: number) => compact.format(value);
const shortDate = (date: string) => new Date(`${date}T00:00:00Z`).toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
const sum = (days: StatsDay[], pick: (day: StatsDay) => number) => days.reduce((total, day) => total + pick(day), 0);

function useDuration() {
  const { t } = useTranslation();
  return (seconds: number) => {
    if (seconds < 3600) return t("stats.minutes", { value: Math.round(seconds / 60) });
    if (seconds < 172800) return t("stats.hours", { value: (seconds / 3600).toFixed(1) });
    return t("stats.days", { value: (seconds / 86400).toFixed(1) });
  };
}

export function StatsView() {
  const { t } = useTranslation();
  const { project, agentById } = useShell();
  const [range, setRange] = useState(30);
  const stats = useStats(project.id, range);
  const duration = useDuration();
  const data = stats.data;

  return (
    <div className="flex h-full flex-col">
      <PageHead title={t("stats.title")}>
        <Segmented value={range} onChange={setRange} options={RANGES.map((days) => ({ value: days, label: t("stats.lastDays", { count: days }) }))} />
      </PageHead>
      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
      <div className="max-w-5xl px-4 pt-3 pb-6">
        <div className="mb-4 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
          <p className="text-fg-2">{t("stats.subtitle")}</p>
          <span className="flex flex-wrap gap-x-4 gap-y-1">
            <a
              href={`/api/projects/${project.id}/work-report?days=${range}`}
              download
              title={t("stats.reportHint")}
              className="text-fg-2 underline decoration-line-2 underline-offset-[3px] hover:text-fg hover:decoration-current"
            >
              {t("stats.report")}
            </a>
            <a
              href={`/api/projects/${project.id}/work-log?days=${range}`}
              download
              title={t("stats.logHint")}
              className="text-fg-2 underline decoration-line-2 underline-offset-[3px] hover:text-fg hover:decoration-current"
            >
              {t("stats.log")}
            </a>
          </span>
        </div>
        {!data ? (
          <div className="flex justify-center py-12">
            <Spinner />
          </div>
        ) : (
          <div className="space-y-7">
            <div className="grid grid-cols-2 gap-x-6 rule-t md:grid-cols-4">
              <Tile label={t("stats.ticketsDone")} value={count(sum(data.days, (d) => d.tickets_done))} />
              <Tile label={t("stats.ticketsCreated")} value={count(sum(data.days, (d) => d.tickets_created))} />
              <Tile label={t("stats.openTickets")} value={count(data.open_tickets)} />
              <Tile label={t("stats.cycleTime")} value={data.median_cycle_seconds === null ? "–" : duration(data.median_cycle_seconds)} hint={t("stats.cycleHint")} />
              <Tile label={t("stats.runs")} value={count(sum(data.days, (d) => d.runs))} hint={t("stats.failedRuns", { count: sum(data.days, (d) => d.failed_runs) })} />
              <Tile label={t("stats.tokens")} value={count(sum(data.days, (d) => d.tokens))} />
              <Tile label={t("stats.cost")} value={money(sum(data.days, (d) => d.cost_usd))} />
            </div>
            <BarChart
              title={t("stats.ticketsChart")}
              days={data.days}
              format={String}
              series={[
                { label: t("stats.ticketsDone"), color: "bg-ok", pick: (d) => d.tickets_done },
                { label: t("stats.ticketsCreated"), color: "bg-fg-3", pick: (d) => d.tickets_created },
              ]}
            />
            <div className="grid gap-x-6 gap-y-7 md:grid-cols-3">
              <BarChart title={t("stats.runsChart")} days={data.days} format={String} series={[{ label: t("stats.runs"), color: "bg-fg", pick: (d) => d.runs }]} />
              <BarChart title={t("stats.tokensChart")} days={data.days} format={count} series={[{ label: t("stats.tokens"), color: "bg-fg", pick: (d) => d.tokens }]} />
              <BarChart title={t("stats.costChart")} days={data.days} format={money} series={[{ label: t("stats.cost"), color: "bg-fg", pick: (d) => d.cost_usd }]} />
            </div>
            <section>
              <h2 className="caps rule-b pb-1.5">{t("stats.agents")}</h2>
              {data.agents.length === 0 ? (
                <p className="text-fg-3">{t("stats.noAgents")}</p>
              ) : (
                <div className="scroll-thin overflow-x-auto">
                  <table className="w-full tabular-nums">
                    <thead>
                      <tr className="border-b border-line-2 text-left text-fg-3 [&>th]:py-2 [&>th]:pr-4 [&>th]:font-normal [&>th]:whitespace-nowrap">
                        <th>{t("stats.agent")}</th>
                        <th>{t("stats.runs")}</th>
                        <th>{t("stats.failed")}</th>
                        <th>{t("stats.assignmentsDone")}</th>
                        <th>{t("stats.busy")}</th>
                        <th>{t("stats.tokens")}</th>
                        <th>{t("stats.cost")}</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-line">
                      {data.agents.map((row) => {
                        const agent = agentById(row.agent_id);
                        return (
                          <tr key={row.agent_id} className="[&>td]:py-2 [&>td]:pr-4 [&>td]:whitespace-nowrap">
                            <td>
                              <span className="flex items-center gap-2 font-medium">
                                {agent && <AgentSquare agent={agent} />}
                                {agent?.name ?? "?"}
                              </span>
                            </td>
                            <td>{row.runs}</td>
                            <td>{row.failed_runs}</td>
                            <td>{row.assignments_done}</td>
                            <td>{duration(row.busy_seconds)}</td>
                            <td>{count(row.tokens)}</td>
                            <td>{money(row.cost_usd)}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          </div>
        )}
      </div>
      </div>
    </div>
  );
}

function Tile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="border-b border-line py-2.5">
      <div className="text-fg-3">{label}</div>
      <div className="font-extrabold">{value}</div>
      {hint && <div className="text-fg-3">{hint}</div>}
    </div>
  );
}

interface Series {
  label: string;
  color: string;
  pick: (day: StatsDay) => number;
}

function BarChart({ title, days, series, format }: { title: string; days: StatsDay[]; series: Series[]; format: (value: number) => string }) {
  const { t } = useTranslation();
  const [hovered, setHovered] = useState<number | null>(null);
  const highest = Math.max(0, ...days.flatMap((day) => series.map((s) => s.pick(day))));
  const place = hovered === null ? 0 : ((hovered + 0.5) / days.length) * 100;

  return (
    <figure className="min-w-0">
      <figcaption className="mb-4 flex flex-wrap items-center justify-between gap-x-4 gap-y-1 rule-b pb-1.5">
        <span className="caps">{title}</span>
        {series.length > 1 && (
          <span className="flex flex-wrap gap-3 text-fg-3">
            {series.map((s) => (
              <span key={s.label} className="flex items-center gap-1.5">
                <span className={clsx("size-[7px] rounded-[1px]", s.color)} />
                {s.label}
              </span>
            ))}
          </span>
        )}
      </figcaption>
      <div className="relative" aria-hidden onMouseLeave={() => setHovered(null)}>
        <div className="absolute inset-x-0 top-0 border-t border-dashed border-line" />
        <span className="absolute top-0.5 left-0 z-[1] bg-bg pr-1 text-fg-3">{format(highest)}</span>
        <div className="flex h-28 items-stretch border-b border-line">
          {days.map((day, index) => (
            <div key={day.date} onMouseEnter={() => setHovered(index)} onClick={() => setHovered(index)} className={clsx("flex min-w-0 flex-1 items-end justify-center gap-px px-px", hovered === index && "bg-sunk")}>
              {series.map((s) => {
                const value = s.pick(day);
                return <div key={s.label} className={clsx("max-w-3 min-w-0 flex-1", s.color)} style={{ height: highest ? `${(value / highest) * 100}%` : 0, minHeight: value > 0 ? 2 : 0 }} />;
              })}
            </div>
          ))}
        </div>
        {hovered !== null && (
          <div className="pointer-events-none absolute top-1 z-10 rounded-sm bg-bg px-2.5 py-1.5 whitespace-nowrap shadow-lg ring-1 ring-line-2" style={{ left: `${place}%`, transform: `translateX(-${place}%)` }}>
            <div className="font-medium">{shortDate(days[hovered].date)}</div>
            {series.map((s) => (
              <div key={s.label} className="flex items-center gap-1.5 text-fg-2">
                <span className={clsx("size-[7px] rounded-[1px]", s.color)} />
                {s.label}: <span className="font-semibold text-fg">{format(s.pick(days[hovered]))}</span>
              </div>
            ))}
          </div>
        )}
        <div className="mt-1 flex justify-between text-fg-3">
          <span>{shortDate(days[0].date)}</span>
          <span>{shortDate(days[days.length - 1].date)}</span>
        </div>
      </div>
      <table className="sr-only">
        <caption>{title}</caption>
        <thead>
          <tr>
            <th>{t("stats.date")}</th>
            {series.map((s) => (
              <th key={s.label}>{s.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {days.map((day) => (
            <tr key={day.date}>
              <td>{shortDate(day.date)}</td>
              {series.map((s) => (
                <td key={s.label}>{format(s.pick(day))}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}
