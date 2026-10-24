"use client";

import { useState } from "react";
import type { ModelInfo } from "@/lib/api/types";
import { fmtDate, pct, scoreText, shortModel, signedFixed } from "@/lib/format";
import { companies } from "@/lib/runs";
import { compare, modelChangeIndexes, type Snapshot } from "@/lib/stock/history";
import { ScoreHistoryChart } from "@/components/charts/ScoreHistoryChart";
import { Card } from "@/components/ui/Card";
import { Notice } from "@/components/ui/Notice";
import { SignalChip } from "@/components/ui/SignalChip";
import s from "./lower.module.css";

function flagsText(p: Snapshot): string {
  const parts = [`${p.redFlags.length} triggered`];
  if (p.unknownFlags.length) parts.push(`${p.unknownFlags.length} unchecked`);
  return parts.join(" · ");
}

function Column({ title, p }: { title: string; p: Snapshot }) {
  return (
    <div className={s.cmpCol}>
      <h4>
        {title} · {fmtDate(p.date)}
      </h4>
      <div className={s.cmpRow}>
        Score <b className="tnum">{p.score === null ? "Not scored" : scoreText(p.score)}</b>
      </div>
      <div className={s.cmpRow}>
        Signal <SignalChip signal={p.signal} />
      </div>
      <div className={s.cmpRow}>
        Coverage <b className="tnum">{p.coverage === null ? "—" : pct(p.coverage, 0)}</b>
      </div>
      <div className={s.cmpRow}>
        Red flags <b>{flagsText(p)}</b>
      </div>
      <div className={s.cmpRow}>
        Model <b className="tnum">{shortModel(p.model)}</b>
      </div>
    </div>
  );
}

function partialSentence(p: Snapshot): string {
  const n = p.runSize!;
  return `${fmtDate(p.date)} was a partial run: only ${companies(n)} ${n === 1 ? "was" : "were"} scored, so the score ranks it against ${n === 1 ? "itself" : `those ${n}`}.`;
}

/**
 * Every stored snapshot, so a visitor can scroll back to the day they bought
 * and compare it with today. The site's answer to "what was the score then?".
 */
export function ScoreHistorySection({ points, model }: { points: Snapshot[]; model: ModelInfo }) {
  const now = points[points.length - 1];
  const earlier = points.slice(0, -1).reverse();
  // Default to the previous snapshot: the latest change, not the oldest.
  const [then, setThen] = useState(earlier[0]?.date ?? "");
  const thenPoint = points.find((p) => p.date === then) ?? null;
  const cmp = thenPoint && now ? compare(thenPoint, now) : null;
  const changes = new Set(modelChangeIndexes(points).map((i) => points[i].date));

  return (
    <section id="history" className={`${s.block}`} style={{ scrollMarginTop: 64 }} aria-labelledby="history-h">
      <h2 className={s.secH} id="history-h">
        Score history <span className={s.secSub}>every date this stock was scored</span>
      </h2>
      <Card>
        <div className={s.chartWrap}>
          <ScoreHistoryChart points={points} thresholds={{ sell: model.sell_threshold, buy: model.buy_threshold }} selected={then || undefined} />
        </div>

        {earlier.length === 0 ? (
          <p className={s.foot}>Only one snapshot so far: first scored {fmtDate(now.date)}. Comparisons appear once it is scored again.</p>
        ) : (
          <>
            <div className={s.histTop}>
              <label htmlFor="compare-with">Compare with</label>
              <select id="compare-with" value={then} onChange={(e) => setThen(e.target.value)}>
                {earlier.map((p) => (
                  <option key={p.date} value={p.date}>
                    {fmtDate(p.date)}
                  </option>
                ))}
              </select>
            </div>
            {thenPoint && cmp && (
              <>
                <div className={s.cmp}>
                  <Column title="Then" p={thenPoint} />
                  <Column title="Now" p={now} />
                </div>
                <p className={s.delta}>
                  Score change:{" "}
                  <b className="tnum">{cmp.scoreDelta === null ? "—" : `${signedFixed(cmp.scoreDelta, 1)} points`}</b>
                  {cmp.signalChanged ? " · the signal changed" : " · same signal"}
                </p>
                {!cmp.comparable && (
                  <Notice tone="warn" className={s.notice}>
                    {cmp.modelChanged && `Scored by different model versions (${shortModel(thenPoint.model)} → ${shortModel(now.model)}). `}
                    {[thenPoint, now]
                      .filter((p) => p.partial)
                      .map((p) => `${partialSentence(p)} `)
                      .join("")}
                    The two scores are not strictly comparable.
                  </Notice>
                )}
              </>
            )}
          </>
        )}
        <div className={s.soonRow}>Family changes: coming soon. Which families moved between two dates needs a past-date breakdown from the API.</div>

        <div className={s.tblWrap} style={{ marginTop: 16 }}>
          <table className={`${s.tbl} tnum`}>
            <thead>
              <tr>
                <th>Date</th>
                <th>Score</th>
                <th className={s.left}>Signal</th>
                <th>Coverage</th>
                <th className={s.left}>Red flags</th>
                <th>Model</th>
              </tr>
            </thead>
            <tbody>
              {[...points].reverse().map((p) => (
                <tr key={p.date}>
                  <td>
                    {fmtDate(p.date)}
                    {p.partial && <small className={s.partialTag}>partial run ({companies(p.runSize!)})</small>}
                  </td>
                  <td>{p.score === null ? "—" : scoreText(p.score)}</td>
                  <td className={s.left}>
                    <SignalChip signal={p.signal} />
                  </td>
                  <td>{p.coverage === null ? "—" : pct(p.coverage, 0)}</td>
                  <td className={s.left}>{flagsText(p)}</td>
                  <td>
                    {shortModel(p.model)}
                    {changes.has(p.date) && " · new"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </section>
  );
}
