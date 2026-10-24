import type { Metadata } from "next";
import { connection } from "next/server";
import { ApiUnavailableError, serverGet } from "@/lib/api/client";
import type { ModelInfo } from "@/lib/api/types";
import { AppShell } from "@/components/shell/AppShell";
import { LearnView } from "@/components/learn/LearnView";

export const metadata: Metadata = { title: "How scores work" };

export default async function LearnPage() {
  await connection();
  const model = await serverGet<ModelInfo>("/model");
  if (!model) throw new ApiUnavailableError(404, "/model");
  return (
    <AppShell active="learn" crumbs={[{ label: "How scores work" }]} freshness={{ scores: model.scored_dates[0] ?? null }}>
      <LearnView model={model} />
    </AppShell>
  );
}
