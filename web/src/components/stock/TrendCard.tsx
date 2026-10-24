import { fmtDate, scoreText, signedFixed } from "@/lib/format";
import { changeSinceFirst, type Snapshot } from "@/lib/stock/history";
import { Sparkline } from "@/components/charts/Sparkline";
import { Card } from "@/components/ui/Card";
import { InfoTip } from "@/components/ui/InfoTip";
import s from "./stock.module.css";

const TIP = "Composite score on every date this stock was scored. A dashed line means the scoring model changed, so points either side aren't strictly comparable.";

export function TrendCard({ points }: { points: Snapshot[] }) {
  const scored = points.filter((p) => p.score !== null);
  if (scored.length === 0) return null;
  const change = changeSinceFirst(points);
  return (
    <Card className={s.aTrend} data-testid="trend-card">
      <div className={s.ct}>
        Score trend <InfoTip text={TIP} />
      </div>
      {scored.length === 1 ? (
        <>
          <div className={`${s.big} tnum`}>{scoreText(scored[0].score!)}</div>
          <div className="asof">First scored {fmtDate(scored[0].date)}</div>
        </>
      ) : (
        <>
          <div className={`${s.big} tnum ${change === null || change === 0 ? "" : change > 0 ? s.up : s.down}`}>
            {change === null ? "—" : signedFixed(change, 1)}
          </div>
          <div className="asof">points since first scored, {fmtDate(points[0].date)}</div>
        </>
      )}
      <Sparkline points={points} />
      <div className="asof">
        {points.length} {points.length === 1 ? "snapshot" : "snapshots"} · scroll back in{" "}
        <a href="#history" style={{ color: "var(--accent-ink)" }}>
          Score history
        </a>
      </div>
    </Card>
  );
}
