import Link from "next/link";
import type { Portfolio } from "@/lib/api/types";
import { count, fmtDate, fmtDay, num } from "@/lib/format";
import { Card } from "@/components/ui/Card";
import { Notice } from "@/components/ui/Notice";
import { DangerZone } from "./DangerZone";
import { HoldingsCard } from "./HoldingsCard";
import { SummaryCard } from "./SummaryCard";
import s from "./portfolio.module.css";

/** "Ratings as of 24 Oct 2026 · 3 holdings carry older ratings · Prices 23 Oct–30 Oct 2026". */
export function asOfLine(p: Portfolio): string {
  const parts: string[] = [];
  if (p.ratings_as_of) parts.push(`Ratings as of ${fmtDate(p.ratings_as_of)}`);
  if (p.older_ratings > 0) {
    parts.push(p.older_ratings === 1 ? "1 holding carries an older rating" : `${num(p.older_ratings)} holdings carry older ratings`);
  }
  if (p.prices_to) {
    const from = p.prices_from ?? p.prices_to;
    if (from === p.prices_to) parts.push(`Prices ${fmtDate(p.prices_to)}`);
    else {
      // The year once, unless the range crosses one.
      const start = from.slice(0, 4) === p.prices_to.slice(0, 4) ? fmtDay(from) : fmtDate(from);
      parts.push(`Prices ${start}–${fmtDate(p.prices_to)}`);
    }
  }
  return parts.join(" · ");
}

function ImportButton() {
  return (
    <Link href="/portfolio/import" className={s.btn}>
      Import from Zerodha
    </Link>
  );
}

/**
 * The signed-in visitor's holdings beside the model's rating of each: the
 * banner after an import, the header, the summary, the grouped holdings and
 * a quiet delete. With no holdings, one card saying what the page is for.
 */
export function PortfolioView({ data, saved }: { data: Portfolio; saved: boolean }) {
  const last = data.last_import;
  const banner = saved && last && (
    <Notice tone="success" className={s.banner}>
      <span aria-hidden="true">✓</span>
      <span>{`Saved ${count(last.rows, "holding")} from ${last.filename} (${last.mode === "add" ? "added to" : "replaced"} your portfolio)`}</span>
    </Notice>
  );

  if (data.holdings.length === 0) {
    return (
      <>
        {banner}
        <div className={s.head}>
          <h1>Your portfolio</h1>
        </div>
        <Card className={s.emptyCard}>
          <p>See your Zerodha holdings beside the model&apos;s latest rating of each stock.</p>
          <ImportButton />
        </Card>
      </>
    );
  }

  const line = asOfLine(data);
  return (
    <>
      {banner}
      <div className={s.head}>
        <div>
          <h1>Your portfolio</h1>
          {line && <p className={s.asOf}>{line}</p>}
        </div>
        <ImportButton />
      </div>
      <SummaryCard data={data} />
      <HoldingsCard holdings={data.holdings} groups={data.groups} />
      <DangerZone />
    </>
  );
}
