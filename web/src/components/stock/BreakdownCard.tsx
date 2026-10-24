import type { Explanation } from "@/lib/api/types";
import { fmtDate, ordinal, pct, shortModel, signedFixed } from "@/lib/format";
import { DivergingBar } from "@/components/charts/DivergingBar";
import { Card } from "@/components/ui/Card";
import { Notice } from "@/components/ui/Notice";
import s from "./lower.module.css";

const cap = (f: string) => f.charAt(0).toUpperCase() + f.slice(1);

function joinNames(names: string[]): string {
  return names.length <= 1 ? names.join("") : `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`;
}

/**
 * One row per family: contribution as a diverging bar and a number,
 * percentile, and the weight the family actually carried. The API's
 * contribution is z × weight ÷ (sum of the measured families' weights), so
 * the weight shown is re-normalised the same way, with the declared weight
 * beside it when they differ.
 */
export function BreakdownCard({ explanation }: { explanation: Explanation }) {
  const fams = explanation.families;
  const measuredWeight = fams.filter((f) => f.z !== null).reduce((sum, f) => sum + f.weight, 0);
  const reweighted = measuredWeight > 0 && Math.abs(measuredWeight - 1) > 1e-9;
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
          <div key={f.family} data-family={f.family} style={{ display: "contents" }}>
            <span className={s.fam} title={f.meaning ?? undefined}>
              {cap(f.family)}
            </span>
            <span className={s.contrib}>
              <DivergingBar value={f.contribution} max={max} />
              <span className={`${s.cval} tnum`}>{f.contribution === null ? "" : signedFixed(f.contribution, 2)}</span>
            </span>
            <span className={`${s.pctl} tnum`}>{f.percentile === null ? <span className={s.nm}>n/m</span> : ordinal(f.percentile)}</span>
            <span className={`${s.w} tnum`}>
              {f.z === null || measuredWeight <= 0 ? (
                <span title={`Not measured; its ${pct(f.weight, 0)} went to the measured families`}>—</span>
              ) : (
                <>
                  <b>{pct(f.weight / measuredWeight, 0)}</b>
                  {reweighted && <small> of {pct(f.weight, 0)}</small>}
                </>
              )}
            </span>
          </div>
        ))}
      </div>
      <p className={s.foot}>
        Percentile within the scored universe on {fmtDate(explanation.as_of)}. Contribution = family z × weight, re-weighted over the measured families.
        {unmeasuredText}
      </p>
    </Card>
  );
}
