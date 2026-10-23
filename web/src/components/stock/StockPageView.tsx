import type { StockPageData } from "@/lib/stock/load";
import { toCloses } from "@/lib/prices";
import { snapshots } from "@/lib/stock/history";
import { scoreState } from "@/lib/stock/state";
import { NewsToneCard } from "./NewsToneCard";
import { PriceCard } from "./PriceCard";
import { RedFlagsCard } from "./RedFlagsCard";
import { ScoreCard } from "./ScoreCard";
import { SectionTabs } from "./SectionTabs";
import { StockHeader } from "./StockHeader";
import { TrendCard } from "./TrendCard";
import { WhyCard } from "./WhyCard";
import s from "./stock.module.css";

/** The whole stock page below the shell. Synchronous: the route loads the data. */
export function StockPageView({ data }: { data: StockPageData }) {
  const { instrument, explanation, model } = data;
  const state = scoreState(explanation, model);
  const points = snapshots(data.history);
  const closes = toCloses(data.prices);
  const lastVolume = data.prices.at(-1)?.volume ?? null;
  const scored = explanation !== null;
  const present = scored ? ["overview", "breakdown", "history", "financials", "news", "peers"] : ["overview", "financials", "news", "peers"];

  return (
    <>
      <StockHeader instrument={instrument} />
      <SectionTabs present={present} />
      <section id="overview" className={`${s.ov} ${s.anchor}`} data-state={scored ? "scored" : "unscored"} aria-label="Overview">
        <ScoreCard
          state={state}
          model={model}
          modelVersion={explanation?.stored_version ?? explanation?.current_version ?? null}
          sector={instrument.sector}
        />
        {scored && <TrendCard points={points} />}
        <PriceCard closes={closes} lastVolume={lastVolume} />
        {scored && (
          <div className={s.aSide}>
            <div className={s.sideH}>Checks</div>
            <NewsToneCard news={explanation.news} families={explanation.families} />
            <RedFlagsCard red={explanation.red_flags} unknown={explanation.unknown_flags} />
          </div>
        )}
        {scored && <WhyCard state={state} explanation={explanation} model={model} />}
      </section>
    </>
  );
}
