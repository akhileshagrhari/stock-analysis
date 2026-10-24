import type { Metadata } from "next";
import { connection } from "next/server";
import { loadRedFlags } from "@/lib/signals/load";
import { AppShell } from "@/components/shell/AppShell";
import { RedFlagsView } from "@/components/learn/RedFlagsView";

export const metadata: Metadata = { title: "Red flags" };

const one = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);

export default async function RedFlagsPage({ searchParams }: PageProps<"/red-flags">) {
  await connection();
  const q = await searchParams;
  const { flagged, asOf, notice, fuller } = await loadRedFlags({ as_of: one(q.as_of) });
  return (
    <AppShell active="red-flags" crumbs={[{ label: "Red flags" }]} freshness={{ scores: asOf }}>
      <RedFlagsView flagged={flagged} asOf={asOf} notice={notice} fuller={fuller} />
    </AppShell>
  );
}
