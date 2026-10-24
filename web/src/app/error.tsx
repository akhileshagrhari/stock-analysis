"use client";

import { ErrorView } from "@/components/shell/ErrorView";

export default function RootError({ retry }: { error: unknown; retry: () => void }) {
  return <ErrorView retry={retry} />;
}
