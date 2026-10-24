import Link from "next/link";
import type { Instrument, Signal } from "@/lib/api/types";
import { LOW_COVERAGE } from "@/config/site";
import { isSignalKey } from "@/config/signals";
import { fmtDate, pct, scoreText } from "@/lib/format";
import { Card } from "@/components/ui/Card";
import { SignalChip } from "@/components/ui/SignalChip";
import s from "./lower.module.css";

/** Same-sector companies scored on the same date. Never silently mixes dates. */
export function PeersSection({ instrument, peers, asOf }: { instrument: Instrument; peers: Signal[]; asOf: string | null }) {
  const others = peers.filter((p) => p.isin !== instrument.isin);
  let body;
  if (!instrument.sector || !asOf) {
    body = <div className={s.emptyLine}>This company has no sector on record, so it has no peer list.</div>;
  } else if (others.length === 0) {
    body = (
      <div className={s.emptyLine}>
        No other {instrument.sector} company was scored on {fmtDate(asOf)}
      </div>
    );
  } else {
    body = (
      <>
        <div className="asof" style={{ marginBottom: 8 }}>
          {instrument.sector} companies scored on {fmtDate(asOf)}
        </div>
        <div className={s.tblWrap}>
          <table className={`${s.tbl} tnum`}>
            <thead>
              <tr>
                <th scope="col">Stock</th>
                <th scope="col">Score</th>
                <th scope="col" className={s.left}>
                  Signal
                </th>
                <th scope="col">Coverage</th>
              </tr>
            </thead>
            <tbody>
              {peers.map((p) => {
                const me = p.isin === instrument.isin;
                return (
                  <tr key={p.isin} aria-current={me ? "true" : undefined}>
                    <td>
                      <div className={s.sym}>
                        {me ? <b>{p.nse_symbol}</b> : <Link href={`/stocks/${encodeURIComponent(p.nse_symbol)}`}>{p.nse_symbol}</Link>}
                        <span>{p.name}</span>
                      </div>
                    </td>
                    <td>{p.composite_score === null ? "Not scored" : scoreText(p.composite_score)}</td>
                    <td className={s.left}>
                      <SignalChip signal={isSignalKey(p.signal) ? p.signal : null} />
                    </td>
                    <td style={p.coverage !== null && p.coverage < LOW_COVERAGE ? { color: "var(--hold)" } : undefined}>
                      {p.coverage === null ? "—" : pct(p.coverage, 0)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </>
    );
  }
  return (
    <section id="peers" className={s.block} style={{ scrollMarginTop: 64 }} aria-labelledby="peers-h">
      <h2 className={s.secH} id="peers-h">
        Peers
      </h2>
      <Card>{body}</Card>
    </section>
  );
}
