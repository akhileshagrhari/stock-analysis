import Link from "next/link";
import type { Instrument } from "@/lib/api/types";
import { UNIVERSE } from "@/config/site";
import { Pill } from "@/components/ui/Pill";
import s from "./stock.module.css";

/** Initials from the company name, since there are no logos: Reliance Industries Ltd. → RIL. */
export function monogram(name: string): string {
  return name
    .split(/[\s&.,-]+/)
    .filter((w) => /^[A-Za-z]/.test(w))
    .slice(0, 3)
    .map((w) => w[0].toUpperCase())
    .join("");
}

export function StockHeader({ instrument }: { instrument: Instrument }) {
  return (
    <div className={s.head}>
      <div className={s.mono} aria-hidden="true">
        {monogram(instrument.name)}
      </div>
      <div>
        <h1>{instrument.nse_symbol}</h1>
        <div className={s.co}>
          <span>{instrument.name}</span>
          {instrument.sector && <Pill>{instrument.sector}</Pill>}
          <Pill>NSE · {UNIVERSE}</Pill>
        </div>
      </div>
      <div className={s.acts}>
        <button type="button" className={s.btn} disabled title="Needs sign-in, coming later">
          ☆ Watchlist <span className={s.soon}>soon</span>
        </button>
        <Link href="/learn" className={`${s.btn} ${s.btnPrimary}`}>
          How this score works
        </Link>
      </div>
    </div>
  );
}
