import Link from "next/link";
import type { Signal } from "@/lib/api/types";
import { fmtDate, scoreText } from "@/lib/format";
import { RED_FLAGS } from "@/config/redflags";
import { SIGNALS } from "@/config/signals";
import { Card } from "@/components/ui/Card";
import s from "./learn.module.css";

/** Companies with a triggered red flag on a date, and what each check is. */
export function RedFlagsView({ flagged, asOf }: { flagged: Signal[]; asOf: string | null }) {
  const date = asOf ? fmtDate(asOf) : "the latest date";
  const describe = (name: string) => RED_FLAGS.find((f) => f.name === name)?.description ?? name.replace(/_/g, " ");
  return (
    <div className={s.page}>
      <h1>Red flags</h1>
      <p className={s.lead}>
        A triggered red flag forces a {SIGNALS.SELL.label}, whatever the factor score. A check the data can&apos;t evaluate is
        shown as unchecked, never as passed.
      </p>
      <div className={s.stack}>
        <Card>
          <h2>Flagged on {date}</h2>
          {flagged.length === 0 ? (
            <p>No company has a triggered red flag on {date}.</p>
          ) : (
            <table className={s.tbl} aria-label="Flagged companies">
              <thead>
                <tr>
                  <th scope="col">Stock</th>
                  <th scope="col">Flag</th>
                  <th scope="col" className={s.num}>
                    Score without it
                  </th>
                </tr>
              </thead>
              <tbody>
                {flagged.map((r) => (
                  <tr key={r.isin}>
                    <td>
                      <Link href={`/stocks/${encodeURIComponent(r.nse_symbol)}`}>{r.nse_symbol}</Link>
                      <span className={s.sub}>{r.name}</span>
                    </td>
                    <td>{r.red_flags.map(describe).join("; ")}</td>
                    <td className={`${s.num} tnum`}>{r.composite_score === null ? "n/m" : scoreText(r.composite_score)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
        <Card>
          <table className={s.tbl} aria-label="Red-flag checks">
            <caption className={s.cap}>The model&apos;s six checks</caption>
            <thead>
              <tr>
                <th scope="col">Check</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {RED_FLAGS.map((f) => (
                <tr key={f.name}>
                  <td>{f.description}</td>
                  <td>{f.reachable ? <span className={s.tag}>Checked when data allows</span> : <span className={s.tag}>No data source yet</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      </div>
    </div>
  );
}
