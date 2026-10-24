import type { Explanation, ModelInfo } from "@/lib/api/types";
import { coverageText, factorCount } from "@/lib/coverage";
import { fmtDate, ordinal } from "@/lib/format";
import { bandGap, whySummary, type ScoreState } from "@/lib/stock/state";
import { ScaleBar } from "@/components/charts/ScaleBar";
import { Card } from "@/components/ui/Card";
import { Notice } from "@/components/ui/Notice";
import { SignalChip } from "@/components/ui/SignalChip";
import { overrideText } from "./ScoreCard";
import s from "./stock.module.css";

const MOVES = 0.05;

function familyName(f: string): string {
  return f.charAt(0).toUpperCase() + f.slice(1);
}

/** The verdict and the reason for it: which families pull which way, and how far to the next band. */
export function WhyCard({ state, explanation, model }: { state: ScoreState; explanation: Explanation; model: ModelInfo }) {
  const movers = explanation.families
    .filter((f) => f.contribution !== null && Math.abs(f.contribution) > MOVES && f.percentile !== null)
    .sort((a, b) => Math.abs(b.contribution!) - Math.abs(a.contribution!));
  const coverage = state.coverage;
  const { measured, total } = factorCount(explanation.families);
  const factors = `${measured} of ${total} factors`;
  return (
    <Card className={s.aWhy} data-testid="why-card">
      <div className={s.whyTop}>
        <SignalChip signal={state.signal} />
        <span className="asof" style={{ fontSize: 13.5, color: "var(--ink2)" }}>
          {fmtDate(explanation.as_of)}
        </span>
      </div>
      {state.overridden && (
        <Notice tone="warn" className={s.gapTop}>
          {overrideText(state.overridden)}
        </Notice>
      )}
      <div className={s.whyIn} style={state.overridden ? { marginTop: 12 } : undefined}>
        <h3>{whySummary(explanation.families)}</h3>
        <div className={s.badges}>
          {movers.map((f) => (
            <span key={f.family} className={s.bdg}>
              <i data-dir={f.contribution! > 0 ? "up" : "down"} aria-hidden="true">
                {f.contribution! > 0 ? "↗" : "↘"}
              </i>
              {`${familyName(f.family)} ${ordinal(f.percentile!)}`}
            </span>
          ))}
          {coverage !== null && (
            <span className={s.cov} data-low={state.lowCoverage ? "" : undefined}>
              {state.lowCoverage ? `${coverageText(coverage)} (${factors}), treat with caution` : `${coverageText(coverage)} · ${factors}`}
            </span>
          )}
        </div>
        {explanation.narrative && <p className={s.narrative}>{explanation.narrative}</p>}
      </div>
      <ScaleBar
        score={state.score}
        thresholds={{ sell: model.sell_threshold, buy: model.buy_threshold }}
        label={state.score === null ? "" : bandGap(state.score, model)}
      />
    </Card>
  );
}
