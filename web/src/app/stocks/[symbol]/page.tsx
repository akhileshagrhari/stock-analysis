import type { Metadata } from "next";
import { notFound, redirect } from "next/navigation";
import { cache } from "react";
import { loadStockPage } from "@/lib/stock/load";
import { AppShell } from "@/components/shell/AppShell";
import { StockPageView } from "@/components/stock/StockPageView";

// One load per request, shared by generateMetadata and the page.
const getStockPage = cache((symbol: string) => loadStockPage(symbol));

function decode(segment: string): string {
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

export async function generateMetadata({ params }: PageProps<"/stocks/[symbol]">): Promise<Metadata> {
  const symbol = decode((await params).symbol).toUpperCase();
  const data = await getStockPage(symbol);
  return data ? { title: `${data.instrument.nse_symbol} · ${data.instrument.name}` } : { title: "Stock not found" };
}

export default async function StockPage({ params }: PageProps<"/stocks/[symbol]">) {
  const symbol = decode((await params).symbol);
  // One canonical URL per stock: /stocks/reliance → /stocks/RELIANCE.
  if (symbol !== symbol.toUpperCase()) redirect(`/stocks/${encodeURIComponent(symbol.toUpperCase())}`);

  const data = await getStockPage(symbol);
  if (!data) notFound();

  const filings = [...data.financials.quarterly, ...data.financials.annual]
    .map((f) => f.filing_date)
    .filter((d): d is string => d !== null)
    .sort()
    .at(-1);

  return (
    <AppShell
      active="stocks"
      crumbs={[{ label: "Stocks", href: "/#search" }, { label: data.instrument.name }]}
      freshness={{ scores: data.explanation?.as_of ?? null, prices: data.prices.at(-1)?.date ?? null, filings: filings ?? null }}
    >
      <StockPageView data={data} />
    </AppShell>
  );
}
