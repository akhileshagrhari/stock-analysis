import type { Portfolio } from "@/lib/api/types";
import { inr, num, pct, roundTo } from "@/lib/format";
import { Card } from "@/components/ui/Card";
import { VerdictNote } from "@/components/ui/VerdictNote";
import { GROUP_ORDER, groupLabel } from "./HoldingsCard";
import s from "./portfolio.module.css";

const ZONE = { BUY: s.zBuy, HOLD: s.zHold, SELL: s.zSell, NOT_RATED: s.zNone } as const;

/** Money with its sign, gains included: +₹82,137.62, −₹2,017.20. */
function signedInr(v: number): string {
  return roundTo(v, 2) > 0 ? `+${inr(v)}` : inr(v);
}

/** What the portfolio is worth, what it cost, and how its value splits across the model's ratings. */
export function SummaryCard({ data }: { data: Portfolio }) {
  const move = roundTo(data.pnl, 2) > 0 ? s.up : roundTo(data.pnl, 2) < 0 ? s.down : "";
  // The holdings card's order, whatever order the API sent.
  const groups = GROUP_ORDER.flatMap((g) => data.groups.filter((x) => x.group === g));
  return (
    <Card className={s.summary}>
      <div className={`${s.kpis} tnum`}>
        <div className={s.kpi}>
          <span>Current value</span>
          <b>{inr(data.value)}</b>
        </div>
        <div className={s.kpi}>
          <span>Invested</span>
          <b>{inr(data.invested)}</b>
        </div>
        <div className={s.kpi}>
          <span>Profit / loss</span>
          <b className={move}>
            {signedInr(data.pnl)}
            {data.pnl_pct !== null && ` · ${pct(data.pnl_pct, 1, true)}`}
          </b>
        </div>
      </div>
      <div className={s.bar} data-testid="share-bar" aria-hidden="true">
        {groups
          .filter((g) => g.share > 0)
          .map((g) => (
            <span key={g.group} className={ZONE[g.group]} style={{ width: `${g.share * 100}%` }} />
          ))}
      </div>
      <ul className={s.legend} aria-label="Share of value by rating">
        {groups.map((g) => (
          <li key={g.group}>
            <i className={ZONE[g.group]} aria-hidden="true" />
            {`${groupLabel(g.group)} ${pct(g.share)}`}
          </li>
        ))}
      </ul>
      <VerdictNote date={data.ratings_as_of} many className={s.verdict} />
      {data.unpriced > 0 && (
        <p className={s.unpriced}>
          {data.unpriced === 1 ? "1 holding has no price and isn't counted." : `${num(data.unpriced)} holdings have no price and aren't counted.`}
        </p>
      )}
    </Card>
  );
}
