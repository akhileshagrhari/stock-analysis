import type { Metadata } from "next";
import { connection } from "next/server";
import { loadRedFlags } from "@/lib/signals/load";
import { AppShell } from "@/components/shell/AppShell";
import { RedFlagsView } from "@/components/learn/RedFlagsView";

export const metadata: Metadata = { title: "Red flags" };

export default async function RedFlagsPage() {
  await connection();
  const { flagged, asOf } = await loadRedFlags();
  return (
    <AppShell active="red-flags" crumbs={[{ label: "Red flags" }]} freshness={{ scores: asOf }}>
      <RedFlagsView flagged={flagged} asOf={asOf} />
    </AppShell>
  );
}
