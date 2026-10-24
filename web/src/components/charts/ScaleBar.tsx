import { SIGNALS } from "@/config/signals";
import { scoreText } from "@/lib/format";
import type { Thresholds } from "./SegmentMeter";
import s from "./charts.module.css";

/** The 0-100 line with the SELL and BUY lines, the score as a dot and the gap to the next band. */
export function ScaleBar({ score, thresholds, label }: { score: number | null; thresholds: Thresholds; label: string }) {
  const has = score !== null && Number.isFinite(score);
  const { sell, buy } = thresholds;
  const track = `linear-gradient(90deg, var(--zone-sell-soft) 0 ${sell}%, var(--zone-hold-soft) ${sell}% ${buy}%, var(--zone-buy-soft) ${buy}% 100%)`;
  const at = has ? Math.max(0, Math.min(100, score)) : 0;
  // The gap pill sits between the dot and the line it is measured to.
  const target = has ? (score < sell ? sell : score < buy ? buy : score) : 0;
  return (
    <div>
      <div className={s.scale} role="img" aria-label={has ? `Score ${scoreText(score)}; ${label}` : "Not scored"}>
        <div className={s.trk} style={{ background: track }} />
        <span className={s.tick} style={{ left: `${sell}%` }} />
        <span className={s.tick} style={{ left: `${buy}%` }} />
        {has && <span className={s.now} data-testid="scale-now" style={{ left: `${at}%` }} />}
        {has && label && (
          <span className={s.gap} style={{ left: `${Math.min(88, Math.max(12, (at + target) / 2))}%` }}>
            {label}
          </span>
        )}
      </div>
      <div className={s.scaleL}>
        <span>
          {SIGNALS.SELL.label} line<b className="tnum">{sell}</b>
        </span>
        <span className={s.mid}>
          Score<b className="tnum">{has ? scoreText(score) : "n/m"}</b>
        </span>
        <span className={s.end}>
          {SIGNALS.BUY.label} line<b className="tnum">{buy}</b>
        </span>
      </div>
    </div>
  );
}
