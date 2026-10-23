import type { Explanation } from "@/lib/api/types";
import { fmtDate, ordinal, shortModel } from "@/lib/format";
import { DivergingBar } from "@/components/charts/DivergingBar";
import { Card } from "@/components/ui/Card";
import { Notice } from "@/components/ui/Notice";
import s from "./lower.module.css";

const cap = (f: string) => f.charAt(0).toUpperCase() + f.slice(1);

function joinNames(names: string[]): string {
  return names.length <= 1 ? names.join("") : `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`;
}

/** One row per family: contribution as a diverging bar, percentile and weight. */
export function BreakdownCard({ explanation }: { explanation: Explanation }) {
  const fams = explanation.families;
  const max = Math.max(0.25, ...fams.map((f) => Math.abs(f.contribution ?? 0)));
  const unmeasured = fams.filter((f) => f.factors_measured === 0).map((f) => f.family);
  const unmeasuredText = unmeasured.length
    ? ` ${cap(joinNames(unmeasured))} ${unmeasured.length === 1 ? "wasn't" : "weren't"} measured, so the others were re-weighted.`
    : "";
  return (
    <Card>
      {explanation.stale && (
        <Notice tone="info" className={s.notice}>
          This breakdown is re-derived with current weights; the stored score came from model{" "}
          {shortModel(explanation.stored_version ?? "an earlier version")}.
        </Notice>
      )}
      <div className={s.kv}>
        <span className={s.h}>Family</span>
        <span className={s.h}>Contribution</span>
        <span className={`${s.h} ${s.right}`}>Pctl</span>
        <span className={`${s.h} ${s.right}`}>Wt</span>
        {fams.map((f) => (
          <div key={f.family} style={{ display: "contents" }}>
            <span className={s.fam} title={f.meaning ?? undefined}>
              {cap(f.family)}
            </span>
            <DivergingBar value={f.contribution} max={max} />
            <span className={`${s.pctl} tnum`}>{f.percentile === null ? <span className={s.nm}>n/m</span> : ordinal(f.percentile)}</span>
            <span className={`${s.w} tnum`}>{Math.round(f.weight * 100)}%</span>
          </div>
        ))}
      </div>
      <p className={s.foot}>
        Percentile within the scored universe on {fmtDate(explanation.as_of)}. Contribution = weight × family z-score.
        {unmeasuredText}
      </p>
    </Card>
  );
}
