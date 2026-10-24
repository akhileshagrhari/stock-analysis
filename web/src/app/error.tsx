"use client";

import { AppShell } from "@/components/shell/AppShell";
import { ErrorView } from "@/components/shell/ErrorView";

export default function RootError({ retry }: { error: unknown; retry: () => void }) {
  return (
    <AppShell freshness={{ scores: null }} crumbs={[{ label: "Data unavailable" }]}>
      <ErrorView retry={retry} />
    </AppShell>
  );
}
