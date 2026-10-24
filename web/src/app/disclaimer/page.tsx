import type { Metadata } from "next";
import { connection } from "next/server";
import { serverGet } from "@/lib/api/client";
import type { Health } from "@/lib/api/types";
import { AppShell } from "@/components/shell/AppShell";
import { DisclaimerView } from "@/components/learn/DisclaimerView";

export const metadata: Metadata = { title: "Disclaimer" };

export default async function DisclaimerPage() {
  await connection();
  // The disclaimer must show even when the API is down.
  const health = await serverGet<Health>("/health").catch(() => null);
  return (
    <AppShell active="disclaimer" crumbs={[{ label: "Disclaimer" }]} freshness={{ scores: health?.latest_as_of ?? null }}>
      <DisclaimerView />
    </AppShell>
  );
}
