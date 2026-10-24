"use client";

import { useState } from "react";
import { daysBetween, fmtDate, fmtDay, inr, num, parseDay } from "@/lib/format";
import type { Close } from "@/lib/prices";
import { niceDomain } from "./scale";
import { useSize } from "./useSize";
import s from "./charts.module.css";

const PAD = { r: 52, b: 24, t: 8 };
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function monthYear(iso: string): string {
  const d = parseDay(iso);
  return `${MONTHS[d.getMonth()]} '${String(d.getFullYear()).slice(2)}`;
}

/** Closing prices with a right-hand axis, a crosshair and a tooltip. Resizes with its box. */
export function PriceChart({ closes }: { closes: Close[] }) {
  const [ref, { width: W, height: H }] = useSize<HTMLDivElement>({ width: 640, height: 300 });
  const [hover, setHover] = useState<number | null>(null);

  if (closes.length === 0) {
    return <div ref={ref} className={s.chartBox} role="img" aria-label="No price data stored" />;
  }

  const n = closes.length;
  const vals = closes.map(([, v]) => v);
  const { lo, hi, step } = niceDomain(vals);
  const plotW = W - PAD.r;
  const x = (i: number) => (n === 1 ? plotW / 2 : (i * plotW) / (n - 1));
  const y = (v: number) => PAD.t + (H - PAD.t - PAD.b) * (1 - (v - lo) / (hi - lo));
  const ticks: number[] = [];
  for (let v = lo; v <= hi + step / 1e6; v += step) ticks.push(Number(v.toFixed(6)));
  const short = daysBetween(closes[0][0], closes[n - 1][0]) <= 100;
  const nt = n === 1 ? 1 : W < 500 ? 3 : 4;
  const dateTicks = Array.from({ length: nt }, (_, k) => (nt === 1 ? 0 : Math.round((k * (n - 1)) / (nt - 1))));
  const path = closes.map(([, v], i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const [firstDate, firstClose] = closes[0];
  const [lastDate, lastClose] = closes[n - 1];
  const summary = `NSE closing price from ${fmtDate(firstDate)} to ${fmtDate(lastDate)}: ${inr(firstClose)} to ${inr(lastClose)}, low ${inr(Math.min(...vals))}, high ${inr(Math.max(...vals))}`;

  const indexAt = (clientX: number, rect: DOMRect) => Math.max(0, Math.min(n - 1, Math.round(((clientX - rect.left) / (rect.width || 1)) * (n - 1))));

  function onKey(e: React.KeyboardEvent) {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    setHover((h) => Math.max(0, Math.min(n - 1, (h ?? n - 1) + (e.key === "ArrowLeft" ? -1 : 1))));
  }

  const h = hover;
  return (
    <div ref={ref} className={s.chartBox}>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={summary} tabIndex={0} onKeyDown={onKey} onBlur={() => setHover(null)}>
        {ticks.map((v) => (
          <g key={v} data-ytick="">
            <line x1={0} x2={plotW} y1={y(v)} y2={y(v)} stroke="var(--grid)" />
            <text x={W} y={y(v) + 4} textAnchor="end" className={s.svgText}>
              {num(v, step < 1 ? 2 : 0)}
            </text>
          </g>
        ))}
        {dateTicks.map((i, k) => (
          <text key={k} x={x(i)} y={H - 4} textAnchor={nt === 1 ? "middle" : k === 0 ? "start" : k === nt - 1 ? "end" : "middle"} className={s.svgText}>
            {short ? fmtDay(closes[i][0]) : monthYear(closes[i][0])}
          </text>
        ))}
        <path data-line="" d={path} fill="none" stroke="var(--price)" strokeWidth={2} strokeLinejoin="round" />
        {n === 1 && <circle cx={x(0)} cy={y(lastClose)} r={4} fill="var(--price)" />}
        {h !== null && (
          <g>
            <line x1={x(h)} x2={x(h)} y1={PAD.t} y2={H - PAD.b} stroke="var(--track)" strokeDasharray="3 3" />
            <circle cx={x(h)} cy={y(closes[h][1])} r={5} fill="var(--price)" stroke="#fff" strokeWidth={2} />
          </g>
        )}
        <rect
          x={0}
          y={0}
          width={plotW}
          height={H}
          fill="transparent"
          onPointerMove={(e) => setHover(indexAt(e.clientX, e.currentTarget.getBoundingClientRect()))}
          onPointerLeave={() => setHover(null)}
        />
      </svg>
      {h !== null && (
        <div className={s.tip} style={{ left: x(h), top: y(closes[h][1]) }}>
          <b>{inr(closes[h][1])}</b> · {fmtDate(closes[h][0])}
        </div>
      )}
    </div>
  );
}
