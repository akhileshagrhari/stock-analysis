import { connection } from "next/server";
import { serverGet } from "@/lib/api/client";
import type { Health } from "@/lib/api/types";
import { AppShell } from "@/components/shell/AppShell";
import { SearchBox } from "@/components/shell/SearchBox";

export default async function Home() {
  await connection();
  const health = await serverGet<Health>("/health");
  return (
    <AppShell active="home" crumbs={[{ label: "Home" }]} freshness={{ scores: health?.latest_as_of ?? null }}>
      <SearchBox />
    </AppShell>
  );
}
