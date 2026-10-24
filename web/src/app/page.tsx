import { connection } from "next/server";
import { loadHome } from "@/lib/signals/load";
import { AppShell } from "@/components/shell/AppShell";
import { HomeView } from "@/components/home/HomeView";

export default async function Home() {
  // Rendered per request (data cached 300s), so `next build` never needs the API.
  await connection();
  const data = await loadHome();
  return (
    <AppShell active="home" crumbs={[{ label: "Home" }]} freshness={{ scores: data.asOf }}>
      <HomeView data={data} />
    </AppShell>
  );
}
