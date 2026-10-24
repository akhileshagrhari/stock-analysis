"use client";

import { useState } from "react";
import { fmtDate, fmtDay, inr, num, pct } from "@/lib/format";
import { PERIODS, priceStats, slicePeriod, type Close, type Period, type Range } from "@/lib/prices";
import { PriceChart } from "@/components/charts/PriceChart";
import { Card } from "@/components/ui/Card";
import { Segmented } from "@/components/ui/Segmented";
import s from "./stock.module.css";

const LABEL: Record<Period, string> = { "1M": "1M", "3M": "3M", "6M": "6M", "1Y": "1Y", "3Y": "3Y", MAX: "Max" };

/** Last close, change over the chosen period, the chart and the 52-week stats (intraday high and low). */
export function PriceCard({ closes, lastVolume, range }: { closes: Close[]; lastVolume: number | null; range: Range | null }) {
  const [period, setPeriod] = useState<Period>("1Y");
  const stats = priceStats(closes, period);

  if (!stats) {
    return (
      <Card className={s.aPrice}>
        <div className={s.ct}>Price</div>
        <div className={s.empty}>No price data stored</div>
      </Card>
    );
  }

  const data = slicePeriod(closes, period);
  const dir = stats.change < 0 ? "down" : stats.change > 0 ? "up" : "flat";
  const since = period === "MAX" ? `since ${fmtDate(closes[0][0])}` : period;
  return (
    <Card className={s.aPrice}>
      <div className={s.pcTop}>
        <span className={`${s.px} tnum`}>{inr(stats.last[1])}</span>
        <span className={`${s.chg} tnum`} data-dir={dir} data-testid="price-change">
          {dir === "down" ? "↘" : dir === "up" ? "↗" : "→"} {pct(Math.abs(stats.changePct), 2)}
        </span>
        <span className={`${s.chgAbs} tnum ${dir === "down" ? s.down : dir === "up" ? s.up : ""}`}>
          {stats.change > 0 ? "+" : ""}
          {inr(stats.change)} · {since}
        </span>
        <span className={`asof ${s.pxAsof}`}>NSE close · {fmtDate(stats.last[0])}</span>
      </div>
      <Segmented
        label="Price period"
        inline
        className={s.periods}
        options={PERIODS.map((p) => ({ value: p, label: LABEL[p] }))}
        value={period}
        onChange={setPeriod}
      />
      <div className={s.chart}>
        <PriceChart closes={data} />
      </div>
      <div className={s.stats}>
        <div>
          52-wk high<b className="tnum">{range ? inr(range.high) : "—"}</b>
        </div>
        <div>
          52-wk low<b className="tnum">{range ? inr(range.low) : "—"}</b>
        </div>
        <div>
          Off 52-wk high<b className="tnum">{range && range.high > 0 ? pct(stats.last[1] / range.high - 1, 1) : "—"}</b>
        </div>
        <div>
          Volume ({fmtDay(stats.last[0])})<b className="tnum">{lastVolume === null ? "—" : `${num(lastVolume / 1e5, 1)} lakh`}</b>
        </div>
      </div>
    </Card>
  );
}
