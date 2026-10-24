import type { ModelInfo } from "@/lib/api/types";
import { fmtDate, pct, scoreText, shortModel } from "@/lib/format";
import type { ScoreState } from "@/lib/stock/state";
import { unscoredMessage } from "@/lib/stock/state";
import { SIGNALS } from "@/config/signals";
import { UNIVERSE } from "@/config/site";
import { SegmentMeter } from "@/components/charts/SegmentMeter";
import { Card } from "@/components/ui/Card";
import { InfoTip } from "@/components/ui/InfoTip";
import { Notice } from "@/components/ui/Notice";
import { SignalChip } from "@/components/ui/SignalChip";
import s from "./stock.module.css";

const TIP = `A relative rank of this stock among scored ${UNIVERSE} stocks on one date, built from factor percentiles. Not a valuation or a price target.`;

export function overrideText(o: NonNullable<ScoreState["overridden"]>): string {
  return `${SIGNALS.SELL.label} forced by red flag: ${o.description}. Without it, the score would read ${SIGNALS[o.wouldBe].label}.`;
}

export function ScoreCard({ state, model, modelVersion, sector }: { state: ScoreState; model: ModelInfo; modelVersion: string | null; sector: string | null }) {
  const thresholds = { sell: model.sell_threshold, buy: model.buy_threshold };
  return (
    <Card className={s.aScore}>
      <div className={s.ct}>
        Composite score <InfoTip text={TIP} />
        {state.kind === "scored" && (
          <span className={s.ctRight}>
            <SignalChip signal={state.signal} />
          </span>
        )}
      </div>
      {state.kind === "scored" ? (
        <>
          <div className={`${s.big} tnum`} data-testid="score-value">
            {scoreText(state.score!)}
            <small> / 100</small>
          </div>
          <SegmentMeter score={state.score} thresholds={thresholds} />
          {state.overridden && (
            <Notice tone="alert" className={s.gapTop}>
              {overrideText(state.overridden)}
            </Notice>
          )}
          <div className="asof" style={{ marginTop: 8 }}>
            Scored {fmtDate(state.asOf!)}
            {modelVersion && ` · model ${shortModel(modelVersion)}`}
          </div>
        </>
      ) : (
        <>
          <div className={`${s.big} ${s.bigGrey}`}>Not scored</div>
          {state.kind === "unscored" ? (
            <p className={s.muted}>{unscoredMessage(sector)}</p>
          ) : (
            <p className={s.muted}>
              {state.coverage === null
                ? `Not scored on ${fmtDate(state.asOf!)}: not enough data.`
                : `Not scored on ${fmtDate(state.asOf!)}: data behind only ${pct(state.coverage, 0)} of the model's weight.`}
            </p>
          )}
        </>
      )}
    </Card>
  );
}
