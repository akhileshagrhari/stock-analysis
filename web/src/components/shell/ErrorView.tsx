"use client";

import Link from "next/link";
import { BRAND } from "@/config/site";
import s from "./status.module.css";

/** Shown when the API is down or mid-publish: never a blank page or a 0. */
export function ErrorView({ retry }: { retry: () => void }) {
  return (
    <div className={s.page}>
      <title>{`Data unavailable | ${BRAND}`}</title>
      <div className={s.box} role="alert">
        <h1>Data is temporarily unavailable</h1>
        <p>We couldn&apos;t reach the scores database. This usually clears within a few minutes.</p>
        <div className={s.acts}>
          <button type="button" className={s.primary} onClick={() => retry()}>
            Try again
          </button>
          <Link href="/">Go to the home page</Link>
        </div>
      </div>
    </div>
  );
}
