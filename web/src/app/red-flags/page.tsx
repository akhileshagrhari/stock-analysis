import type { Metadata } from "next";
import { connection } from "next/server";
import { serverGet } from "@/lib/api/client";
import type { Health, Signal } from "@/lib/api/types";
import { AppShell } from "@/components/shell/AppShell";
import { RedFlagsView } from "@/components/learn/RedFlagsView";

export const metadata: Metadata = { title: "Red flags" };

export default async function RedFlagsPage() {
  await connection();
  const [health, flagged] = await Promise.all([serverGet<Health>("/health"), serverGet<Signal[]>("/signals/red-flags?limit=1000")]);
  const asOf = health?.latest_as_of ?? null;
  return (
    <AppShell active="red-flags" crumbs={[{ label: "Red flags" }]} freshness={{ scores: asOf }}>
      <RedFlagsView flagged={flagged ?? []} asOf={asOf} />
    </AppShell>
  );
}
