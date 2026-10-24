import Link from "next/link";
import type { Signal } from "@/lib/api/types";
import { fmtDate, scoreText } from "@/lib/format";
import type { HomeData } from "@/lib/signals/load";
import { UNIVERSE } from "@/config/site";
import { isSignalKey, SIGNALS } from "@/config/signals";
import { SearchBox } from "@/components/shell/SearchBox";
import { Card } from "@/components/ui/Card";
import { Notice } from "@/components/ui/Notice";
import { SignalChip } from "@/components/ui/SignalChip";
import { VerdictNote } from "@/components/ui/VerdictNote";
import s from "./home.module.css";

function StockList({ rows, testId, empty }: { rows: Signal[]; testId: string; empty: string }) {
  if (rows.length === 0) return <div className={s.empty}>{empty}</div>;
  return (
    <ul className={s.list} data-testid={testId}>
      {rows.map((r) => (
        <li key={r.isin}>
          <Link href={`/stocks/${encodeURIComponent(r.nse_symbol)}`}>
            <b>{r.nse_symbol}</b>
            <span className={s.name}>{r.name}</span>
            <span className={`${s.score} tnum`}>{r.composite_score === null ? "n/m" : scoreText(r.composite_score)}</span>
            <SignalChip signal={isSignalKey(r.signal) ? r.signal : null} />
          </Link>
        </li>
      ))}
    </ul>
  );
}

export function HomeView({ data }: { data: HomeData }) {
  const { counts, model } = data;
  const date = fmtDate(data.asOf);
  const listsDate = fmtDate(data.listsAsOf);
  return (
    <>
      <section id="search" className={s.hero}>
        <h1>Search {UNIVERSE} stocks</h1>
        <p>
          Every {UNIVERSE} company our model scores, with the factors behind its score and a history of how it changed.
        </p>
        <SearchBox />
      </section>

      <div className={s.grid}>
        <Card className={s.wide}>
          <h2 className={s.h2}>
            Latest scores ({date}) <Link href="/signals">All signals</Link>
          </h2>
          <div className={`${s.counts} tnum`} data-testid="today-counts">
            <span className={s.pos}>
              {SIGNALS.BUY.label} {counts.BUY}
            </span>
            <span className={s.sep}> · </span>
            <span className={s.hold}>
              {SIGNALS.HOLD.label} {counts.HOLD}
            </span>
            <span className={s.sep}> · </span>
            <span className={s.neg}>
              {SIGNALS.SELL.label} {counts.SELL}
            </span>
          </div>
          <p className={s.sub}>
            {data.scored} of {data.instruments} companies scored on {date}; {data.notScored} not scored on that date.
          </p>
          {data.notice && (
            <Notice tone="warn" className={s.notice}>
              <span>{data.notice}</span>
              {data.listsAsOf !== data.asOf && <span>The lists below use {listsDate}.</span>}
              {data.fuller && <Link href={`/signals?as_of=${data.fuller.date}`}>See the {fmtDate(data.fuller.date)} list</Link>}
            </Notice>
          )}
        </Card>

        <div className={s.wide} data-testid="lists-note">
          <VerdictNote date={data.listsAsOf} many className={s.listsNote} />
        </div>
        <Card>
          <h2 className={s.h2}>
            Highest scores <span className="asof">{listsDate}</span>
          </h2>
          <StockList rows={data.top} testId="top-list" empty={`Nothing scored on ${listsDate}.`} />
        </Card>
        <Card>
          <h2 className={s.h2}>
            Lowest scores <span className="asof">{listsDate}</span>
          </h2>
          <StockList rows={data.bottom} testId="bottom-list" empty={`Too few scored on ${listsDate} for a separate list.`} />
        </Card>
        <Card>
          <h2 className={s.h2}>
            Red flags <span className="asof">{listsDate}</span> <Link href="/red-flags">All</Link>
          </h2>
          <StockList rows={data.flagged} testId="flag-list" empty={`No red flag triggered on ${listsDate}.`} />
        </Card>

        <Card className={s.wide}>
          <h2 className={s.h2}>
            How scores work <Link href="/learn">Learn more</Link>
          </h2>
          <div className={s.weights}>
            {Object.entries(model.family_weights).map(([family, w]) => (
              <div key={family} style={{ display: "contents" }}>
                <span>{family}</span>
                <i style={{ width: `${Math.round(w * 100 * 2)}%` }} aria-hidden="true" />
                <span className="tnum">{Math.round(w * 100)}%</span>
              </div>
            ))}
          </div>
          <p className={s.small}>
            Scores are normal-curve percentiles relative to the companies scored that day, not valuations. {SIGNALS.BUY.label} from{" "}
            {model.buy_threshold}, {SIGNALS.SELL.label} below {model.sell_threshold}; a triggered red flag forces{" "}
            {SIGNALS.SELL.label}.
          </p>
        </Card>
      </div>
    </>
  );
}
