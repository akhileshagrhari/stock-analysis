import { SIGNALS, type SignalKey } from "@/config/signals";
import { scoreText } from "@/lib/format";
import s from "./charts.module.css";

export type Thresholds = { sell: number; buy: number };

function zoneOf(lo: number, t: Thresholds): SignalKey {
  return lo < t.sell ? "SELL" : lo < t.buy ? "HOLD" : "BUY";
}

/** 20 segments of 5 points, coloured by band, filled up to the score. */
export function SegmentMeter({ score, thresholds }: { score: number | null; thresholds: Thresholds }) {
  const has = score !== null && Number.isFinite(score);
  const label = has
    ? `Score ${scoreText(score)} out of 100. ${SIGNALS.SELL.label} below ${thresholds.sell}, ${SIGNALS.BUY.label} from ${thresholds.buy}.`
    : "Not scored";
  return (
    <div>
      <div className={s.meter} role="img" aria-label={label}>
        <div className={s.segs}>
          {Array.from({ length: 20 }, (_, i) => {
            const lo = i * 5;
            return <i key={i} data-zone={zoneOf(lo, thresholds)} data-filled={has && lo < score ? "" : undefined} />;
          })}
        </div>
        {has && (
          <span className={s.marker} data-testid="meter-marker" style={{ left: `${Math.max(0, Math.min(100, score))}%` }} aria-hidden="true">
            ▼
          </span>
        )}
      </div>
      <div className={s.meterL} aria-hidden="true">
        <span>
          {SIGNALS.SELL.label} &lt; {thresholds.sell}
        </span>
        <span>{SIGNALS.HOLD.label}</span>
        <span>
          {SIGNALS.BUY.label} ≥ {thresholds.buy}
        </span>
      </div>
    </div>
  );
}
