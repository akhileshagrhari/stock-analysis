"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { UNIVERSE } from "@/config/site";
import { SearchBox } from "@/components/shell/SearchBox";
import s from "@/components/shell/status.module.css";

/** Unknown symbol: say which, and offer search. */
export function NotFoundView() {
  const params = useParams<{ symbol?: string }>();
  let symbol = params?.symbol ?? "";
  try {
    symbol = decodeURIComponent(symbol);
  } catch {}
  return (
    <div className={s.page}>
      <div className={s.box}>
        <h1>{symbol ? `No ${UNIVERSE} company with symbol ${symbol.toUpperCase()}` : "Page not found"}</h1>
        <p>Search for a company by its NSE symbol or name.</p>
        <div className={s.search}>
          <SearchBox autoFocus />
        </div>
        <Link href="/">Go to the home page</Link>
      </div>
    </div>
  );
}
