import type { Metadata } from "next";
import { loadSignals } from "@/lib/signals/load";
import { AppShell } from "@/components/shell/AppShell";
import { SignalsView } from "@/components/signals/SignalsView";

export const metadata: Metadata = { title: "Signals" };

const one = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);

export default async function SignalsPage({ searchParams }: PageProps<"/signals">) {
  const q = await searchParams;
  const data = await loadSignals({ signal: one(q.signal), sector: one(q.sector), as_of: one(q.as_of) });
  return (
    <AppShell active="signals" crumbs={[{ label: "Signals" }]} freshness={{ scores: data.dates[0] ?? null }}>
      <SignalsView data={data} focusSector={one(q.focus) === "sector"} />
    </AppShell>
  );
}
