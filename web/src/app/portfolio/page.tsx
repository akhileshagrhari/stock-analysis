import type { Metadata } from "next";
import { notFound, redirect } from "next/navigation";
import { connection } from "next/server";
import { PortfolioView } from "@/components/portfolio/PortfolioView";
import { AppShell } from "@/components/shell/AppShell";
import { ApiUnavailableError } from "@/lib/api/client";
import type { Portfolio } from "@/lib/api/types";
import { SignedOutError, userGet } from "@/lib/api/user";
import { accountsEnabled } from "@/lib/auth/config";

export const metadata: Metadata = { title: "Portfolio" };

const PATH = "/me/portfolio";

export default async function PortfolioPage({ searchParams }: PageProps<"/portfolio">) {
  // Per visitor: never prerendered or cached.
  await connection();
  if (!accountsEnabled()) notFound();
  let data: Portfolio | null;
  try {
    data = await userGet<Portfolio>(PATH);
  } catch (error) {
    if (error instanceof SignedOutError) redirect("/login?next=/portfolio");
    // An outage goes to the error page, never to an empty portfolio.
    throw error;
  }
  // The route has no 404 of its own, so a null is a misrouted API.
  if (!data) throw new ApiUnavailableError(404, PATH);

  const q = await searchParams;
  const savedId = Array.isArray(q.saved) ? q.saved[0] : q.saved;
  const saved = data.last_import !== null && savedId === String(data.last_import.id);
  return (
    <AppShell active="portfolio" crumbs={[{ label: "Portfolio" }]} freshness={{ scores: data.ratings_as_of }}>
      <PortfolioView data={data} saved={saved} />
    </AppShell>
  );
}
