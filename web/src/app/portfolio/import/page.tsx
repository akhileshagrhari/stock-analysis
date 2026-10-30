import type { Metadata } from "next";
import { notFound, redirect } from "next/navigation";
import { connection } from "next/server";
import { ImportForm } from "@/components/portfolio/ImportForm";
import { AppShell } from "@/components/shell/AppShell";
import { serverGet } from "@/lib/api/client";
import { SignedOutError, userGet } from "@/lib/api/user";
import type { Health } from "@/lib/api/types";
import { accountsEnabled } from "@/lib/auth/config";
import { currentUser } from "@/lib/auth/server";

export const metadata: Metadata = { title: "Import from Zerodha" };

export default async function ImportPage() {
  await connection();
  if (!accountsEnabled()) notFound();
  if (!(await currentUser())) redirect("/login?next=/portfolio/import");
  // Only to choose the default mode; until the portfolio route exists the API
  // answers 404 "Not Found", which userGet reads as no holdings.
  let hasHoldings = false;
  try {
    const saved = await userGet<{ holdings?: unknown[] }>("/me/portfolio");
    hasHoldings = Boolean(saved?.holdings?.length);
  } catch (error) {
    if (error instanceof SignedOutError) redirect("/login?next=/portfolio/import");
    // Any other trouble only costs the default; saving reports its own.
  }
  const health = await serverGet<Health>("/health").catch(() => null);
  return (
    <AppShell
      active="portfolio"
      crumbs={[{ label: "Portfolio", href: "/portfolio" }, { label: "Import" }]}
      freshness={{ scores: health?.latest_as_of ?? null }}
    >
      <ImportForm hasHoldings={hasHoldings} />
    </AppShell>
  );
}
