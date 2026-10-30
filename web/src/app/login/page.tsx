import type { Metadata } from "next";
import { notFound, redirect } from "next/navigation";
import { connection } from "next/server";
import { LoginView } from "@/components/account/LoginView";
import { AppShell } from "@/components/shell/AppShell";
import { serverGet } from "@/lib/api/client";
import type { Health } from "@/lib/api/types";
import { accountsEnabled, safeNext } from "@/lib/auth/config";
import { emailCodesAvailable } from "@/lib/auth/email";
import { currentUser } from "@/lib/auth/server";

export const metadata: Metadata = { title: "Sign in" };

export default async function LoginPage({ searchParams }: PageProps<"/login">) {
  await connection();
  if (!accountsEnabled()) notFound();
  const q = await searchParams;
  const next = safeNext(Array.isArray(q.next) ? q.next[0] : q.next);
  if (await currentUser()) redirect(next);
  const health = await serverGet<Health>("/health").catch(() => null);
  return (
    <AppShell crumbs={[{ label: "Sign in" }]} freshness={{ scores: health?.latest_as_of ?? null }}>
      <LoginView
        google={Boolean(process.env.GOOGLE_CLIENT_ID && process.env.GOOGLE_CLIENT_SECRET)}
        emailCodes={emailCodesAvailable()}
        next={next}
      />
    </AppShell>
  );
}
