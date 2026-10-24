import type { StockPageData } from "@/lib/stock/load";
import { toCloses } from "@/lib/prices";
import { isPartialRun } from "@/lib/runs";
import { snapshots } from "@/lib/stock/history";
import { scoreState } from "@/lib/stock/state";
import { BreakdownCard } from "./BreakdownCard";
import { DriversCard } from "./DriversCard";
import { FinancialsSection } from "./FinancialsSection";
import { NewsCard } from "./NewsCard";
import { NewsToneCard } from "./NewsToneCard";
import { PeersSection } from "./PeersSection";
import { ScoreHistorySection } from "./ScoreHistorySection";
import { PriceCard } from "./PriceCard";
import { RedFlagsCard } from "./RedFlagsCard";
import { ScoreCard } from "./ScoreCard";
import { SectionTabs } from "./SectionTabs";
import { StockHeader } from "./StockHeader";
import { TrendCard } from "./TrendCard";
import { WhyCard } from "./WhyCard";
import l from "./lower.module.css";
import s from "./stock.module.css";

/** The whole stock page below the shell. Synchronous: the route loads the data. */
export function StockPageView({ data }: { data: StockPageData }) {
  const { instrument, explanation, model } = data;
  const state = scoreState(explanation, model);
  const points = snapshots(data.history, data.runs);
  const current = points.find((p) => p.date === explanation?.as_of) ?? null;
  const peersRun = data.peersAsOf ? data.runs.counts[data.peersAsOf] : undefined;
  const closes = toCloses(data.prices);
  const lastVolume = data.prices.at(-1)?.volume ?? null;
  const scored = explanation !== null;
  const present = ["overview", ...(scored ? ["breakdown"] : []), ...(scored && points.length ? ["history"] : []), "financials", "news", "peers"];

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
          partialRun={current?.partial ? current.runSize : null}
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

      <div className={l.bottom} data-n={scored ? 3 : 1}>
        {scored && (
          <section id="breakdown" className={s.anchor} aria-labelledby="breakdown-h">
            <h2 className={l.secH} id="breakdown-h">
              Score breakdown
            </h2>
            <BreakdownCard explanation={explanation} />
          </section>
        )}
        {scored && (
          <section aria-labelledby="drivers-h">
            <h2 className={l.secH} id="drivers-h">
              Factor drivers
            </h2>
            <DriversCard strengths={explanation.strengths} weaknesses={explanation.weaknesses} />
          </section>
        )}
        <section id="news" className={s.anchor} aria-labelledby="news-h">
          <h2 className={l.secH} id="news-h">
            News
          </h2>
          <NewsCard news={data.news} />
        </section>
      </div>

      {scored && points.length > 0 && <ScoreHistorySection points={points} model={model} />}
      <FinancialsSection financials={data.financials} />
      <PeersSection
        instrument={instrument}
        peers={data.peers}
        asOf={data.peersAsOf}
        scored={explanation !== null}
        partialRun={peersRun !== undefined && isPartialRun(peersRun, data.runs.instruments) ? peersRun : null}
      />
    </>
  );
}
