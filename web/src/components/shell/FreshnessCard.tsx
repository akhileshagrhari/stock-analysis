import { STALE_AFTER_DAYS } from "@/config/site";
import { daysBetween, fmtDate } from "@/lib/format";
import { InfoTip } from "@/components/ui/InfoTip";
import s from "./shell.module.css";

type Props = { today: string; scores: string | null; prices?: string | null; filings?: string | null };

/** When each kind of data was last refreshed. Amber past STALE_AFTER_DAYS. */
export function FreshnessCard({ today, scores, prices, filings }: Props) {
  const rows: [string, string | null | undefined][] = [
    ["Prices", prices],
    ["Scores", scores],
    ["Filings", filings],
  ];
  return (
    <div className={s.fresh}>
      <div className={s.t}>
        Data freshness
        <InfoTip text="Each kind of data is refreshed on its own schedule. Every number on a page carries its own date." />
      </div>
      {rows
        .filter(([label, date]) => date !== undefined || label === "Scores")
        .map(([label, date]) => {
          const stale = date ? (daysBetween(date, today) > STALE_AFTER_DAYS ? "true" : "false") : "unknown";
          return (
            <div key={label} className={s.r} data-stale={stale}>
              <span>
                <span className={s.dot} aria-hidden="true" />
                {label}
              </span>
              <span className="tnum">{date ? fmtDate(date) : "—"}</span>
            </div>
          );
        })}
    </div>
  );
}
