import { fmtDate, fmtDay } from "@/lib/format";
import { modelChangeIndexes, type Snapshot } from "@/lib/stock/history";
import { signalLabel } from "@/config/signals";
import s from "./charts.module.css";

const W = 240;
const H = 78;

/** Every snapshot's score, with a dashed line wherever the scoring model changed. */
export function Sparkline({ points }: { points: Snapshot[] }) {
  const scored = points.filter((p) => p.score !== null) as (Snapshot & { score: number })[];
  if (scored.length === 0) return null;
  const vals = scored.map((p) => p.score);
  const lo = Math.max(0, Math.floor((Math.min(...vals) - 4) / 5) * 5);
  const hi = Math.min(100, Math.ceil((Math.max(...vals) + 4) / 5) * 5);
  const x = (i: number) => (points.length === 1 ? W / 2 : 8 + (i * (W - 16)) / (points.length - 1));
  const y = (v: number) => 6 + (H - 12) * (1 - (v - lo) / (hi - lo || 1));
  const idx = points.map((p, i) => (p.score === null ? -1 : i)).filter((i) => i >= 0);
  const line = idx.map((i, k) => `${k ? "L" : "M"}${x(i).toFixed(1)},${y(points[i].score!).toFixed(1)}`).join(" ");
  const changes = modelChangeIndexes(points);
  const labelAll = points.length <= 4;
  const first = scored[0];
  const last = scored[scored.length - 1];
  const summary =
    scored.length === 1
      ? `Scored once: ${first.score.toFixed(1)} on ${fmtDate(first.date)}`
      : `Score on ${scored.length} dates, from ${first.score.toFixed(1)} on ${fmtDate(first.date)} to ${last.score.toFixed(1)} on ${fmtDate(last.date)}` +
        (changes.length ? `; the model changed ${changes.length === 1 ? "once" : `${changes.length} times`}` : "");

  return (
    <svg viewBox={`0 0 ${W} ${H + 16}`} width="100%" height={H + 16} role="img" aria-label={summary} style={{ display: "block", overflow: "visible", marginTop: 6 }}>
      {changes.map((i, k) => {
        const mx = (x(i) + x(i - 1)) / 2;
        return (
          <g key={i} data-model-change="">
            <line x1={mx} x2={mx} y1={0} y2={H} stroke="var(--track)" strokeDasharray="3 3" />
            {k === 0 && (
              <text x={mx + 4} y={10} className={s.svgSmall}>
                model updated
              </text>
            )}
          </g>
        );
      })}
      {idx.length > 1 && (
        <>
          <path d={`${line} L${x(idx[idx.length - 1])},${H} L${x(idx[0])},${H} Z`} fill="var(--accent)" opacity={0.08} />
          <path d={line} fill="none" stroke="var(--accent)" strokeWidth={2} strokeLinejoin="round" />
        </>
      )}
      {idx.map((i, k) => {
        const p = points[i];
        const anchor = points.length === 1 ? "middle" : i === 0 ? "start" : i === points.length - 1 ? "end" : "middle";
        return (
          <g key={p.date}>
            <circle cx={x(i)} cy={y(p.score!)} r={4} fill="#fff" stroke="var(--accent)" strokeWidth={2}>
              <title>{`${fmtDate(p.date)} · ${p.score!.toFixed(1)} · ${signalLabel(p.signal)}`}</title>
            </circle>
            {(labelAll || k === 0 || k === idx.length - 1) && (
              <text x={x(i)} y={H + 12} textAnchor={anchor} className={s.svgSmall}>
                {fmtDay(p.date)} · {Math.round(p.score!)}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}
