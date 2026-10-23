"use client";

import { useState } from "react";
import { fmtDate, fmtDay, daysBetween, pct, shortModel } from "@/lib/format";
import { modelChangeIndexes, type Snapshot } from "@/lib/stock/history";
import { SIGNALS, signalLabel } from "@/config/signals";
import type { Thresholds } from "./SegmentMeter";
import { useSize } from "./useSize";
import s from "./charts.module.css";

const PAD = { l: 30, r: 14, t: 10, b: 26 };

/** Score (0-100) on every snapshot date, over the SELL / HOLD / BUY bands. */
export function ScoreHistoryChart({ points, thresholds, selected }: { points: Snapshot[]; thresholds: Thresholds; selected?: string }) {
  const [ref, { width: W, height: H }] = useSize<HTMLDivElement>({ width: 640, height: 240 });
  const [hover, setHover] = useState<number | null>(null);
  if (points.length === 0) return null;

  const first = points[0].date;
  const span = daysBetween(first, points[points.length - 1].date);
  const x = (i: number) => {
    if (points.length === 1 || span === 0) return PAD.l + (W - PAD.l - PAD.r) / 2;
    return PAD.l + ((W - PAD.l - PAD.r) * daysBetween(first, points[i].date)) / span;
  };
  const y = (v: number) => PAD.t + (H - PAD.t - PAD.b) * (1 - v / 100);
  const scoredIdx = points.map((p, i) => (p.score === null ? -1 : i)).filter((i) => i >= 0);

  // Lines break at a missing score rather than bridging it.
  const runs: number[][] = [];
  points.forEach((p, i) => {
    if (p.score === null) runs.push([]);
    else if (runs.length === 0) runs.push([i]);
    else runs[runs.length - 1].push(i);
  });
  const bands = [
    { key: "SELL", lo: 0, hi: thresholds.sell, fill: "var(--zone-sell-soft)" },
    { key: "HOLD", lo: thresholds.sell, hi: thresholds.buy, fill: "var(--zone-hold-soft)" },
    { key: "BUY", lo: thresholds.buy, hi: 100, fill: "var(--zone-buy-soft)" },
  ] as const;
  const changes = modelChangeIndexes(points);
  const summary = `Score history: ${scoredIdx.length} scored ${scoredIdx.length === 1 ? "date" : "dates"} from ${fmtDate(first)} to ${fmtDate(points[points.length - 1].date)}. The table below lists every snapshot.`;

  function onMove(e: React.PointerEvent<SVGRectElement>) {
    const r = e.currentTarget.getBoundingClientRect();
    const px = e.clientX - r.left + PAD.l;
    let best = -1;
    let dist = Infinity;
    for (const i of scoredIdx) {
      const d = Math.abs(x(i) - px);
      if (d < dist) [best, dist] = [i, d];
    }
    setHover(best >= 0 ? best : null);
  }

  const hp = hover !== null ? points[hover] : null;
  return (
    <div ref={ref} className={s.chartBox} style={{ height: 240 }}>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={summary}>
        {bands.map((b) => (
          <g key={b.key} data-band={b.key}>
            <rect x={PAD.l} width={W - PAD.l - PAD.r} y={y(b.hi)} height={y(b.lo) - y(b.hi)} fill={b.fill} opacity={0.35} />
            <text x={W - PAD.r - 6} y={y(b.hi) + 14} textAnchor="end" className={s.svgText}>
              {SIGNALS[b.key].label}
            </text>
          </g>
        ))}
        {[0, thresholds.sell, thresholds.buy, 100].map((v) => (
          <g key={v}>
            <line x1={PAD.l} x2={W - PAD.r} y1={y(v)} y2={y(v)} stroke="var(--grid)" />
            <text x={PAD.l - 6} y={y(v) + 4} textAnchor="end" className={s.svgText}>
              {v}
            </text>
          </g>
        ))}
        {changes.map((i) => {
          const mx = (x(i) + x(i - 1)) / 2;
          return (
            <g key={i} data-model-change="">
              <line x1={mx} x2={mx} y1={PAD.t} y2={H - PAD.b} stroke="var(--ink3)" strokeDasharray="4 4" />
              <text x={mx + 4} y={PAD.t + 10} className={s.svgSmall}>
                model updated
              </text>
            </g>
          );
        })}
        {runs
          .filter((r) => r.length > 1)
          .map((r) => (
            <path
              key={r[0]}
              d={r.map((i, k) => `${k ? "L" : "M"}${x(i).toFixed(1)},${y(points[i].score!).toFixed(1)}`).join(" ")}
              fill="none"
              stroke="var(--accent)"
              strokeWidth={2}
              strokeLinejoin="round"
            />
          ))}
        {scoredIdx.map((i) => {
          const p = points[i];
          const sel = p.date === selected;
          return (
            <circle key={p.date} data-point="" cx={x(i)} cy={y(p.score!)} r={sel || hover === i ? 6 : 4.5} fill={sel ? "var(--accent)" : "#fff"} stroke="var(--accent)" strokeWidth={2}>
              <title>{`${fmtDate(p.date)} · ${p.score!.toFixed(1)} · ${signalLabel(p.signal)} · ${p.coverage === null ? "coverage unknown" : `${pct(p.coverage, 0)} of factors`} · model ${shortModel(p.model)}`}</title>
            </circle>
          );
        })}
        {scoredIdx.map((i, k) =>
          scoredIdx.length <= 6 || k === 0 || k === scoredIdx.length - 1 ? (
            <text key={points[i].date} x={x(i)} y={H - 6} textAnchor={k === 0 && scoredIdx.length > 1 ? "start" : k === scoredIdx.length - 1 && scoredIdx.length > 1 ? "end" : "middle"} className={s.svgText}>
              {fmtDay(points[i].date)}
            </text>
          ) : null,
        )}
        <rect x={PAD.l} y={0} width={W - PAD.l - PAD.r} height={H} fill="transparent" onPointerMove={onMove} onPointerLeave={() => setHover(null)} />
      </svg>
      {hp && hp.score !== null && (
        <div className={s.tip} style={{ left: x(hover!), top: y(hp.score) }}>
          <b>{hp.score.toFixed(1)}</b> · {signalLabel(hp.signal)} · {fmtDate(hp.date)}
          <br />
          {hp.coverage === null ? "coverage unknown" : `${pct(hp.coverage, 0)} of factors`} · model {shortModel(hp.model)}
        </div>
      )}
    </div>
  );
}
