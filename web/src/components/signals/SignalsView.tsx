"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { fmtDate, pct, scoreText, signedFixed } from "@/lib/format";
import type { SignalsData } from "@/lib/signals/load";
import { sortRows, type Row, type SortKey } from "@/lib/signals/list";
import { LOW_COVERAGE } from "@/config/site";
import { isSignalKey, SIGNALS, type SignalKey } from "@/config/signals";
import { Card } from "@/components/ui/Card";
import { Notice } from "@/components/ui/Notice";
import { Segmented } from "@/components/ui/Segmented";
import { SignalChip } from "@/components/ui/SignalChip";
import s from "./signals.module.css";

type Filters = { as_of: string | null; signal: SignalKey | null; sector: string | null };

function href(f: Filters): string {
  const p = new URLSearchParams();
  if (f.as_of) p.set("as_of", f.as_of);
  if (f.signal) p.set("signal", f.signal);
  if (f.sector) p.set("sector", f.sector);
  const q = p.toString();
  return q ? `/signals?${q}` : "/signals";
}

function flagsText(r: Row): string {
  const n = r.red_flags.length;
  const u = r.unknown_flags.length;
  return `${n}${u ? ` · ?${u}` : ""}`;
}

function Change({ r, previousAsOf }: { r: Row; previousAsOf: string | null }) {
  if (r.delta === null) return <span className="srOnly">{previousAsOf ? `Not scored on ${fmtDate(previousAsOf)}` : "No earlier date"}</span>;
  return (
    <>
      <span className={r.delta > 0 ? s.up : r.delta < 0 ? s.down : ""}>{signedFixed(r.delta, 1)}</span>
      {r.modelChanged && (
        <span className={s.dagger} title={`Scored by a different model version on ${previousAsOf ? fmtDate(previousAsOf) : "the earlier date"}, so not strictly comparable`}>
          †
        </span>
      )}
    </>
  );
}

/** Every stock scored on one date, filterable and sortable; filters live in the URL. */
export function SignalsView({ data, focusSector = false }: { data: SignalsData; focusSector?: boolean }) {
  const router = useRouter();
  const [sort, setSort] = useState<{ key: SortKey; dir: "asc" | "desc" }>({ key: "score", dir: "desc" });
  const latest = data.dates[0];
  const current: Filters = { as_of: data.asOf === latest ? null : data.asOf, signal: data.filters.signal, sector: data.filters.sector };
  const go = (patch: Partial<Filters>) => router.replace(href({ ...current, ...patch }), { scroll: false });
  const rows = sortRows(data.rows, sort.key, sort.dir);
  const filtered = data.filters.signal !== null || data.filters.sector !== null;

  function sortBy(key: SortKey) {
    setSort((p) => (p.key === key ? { key, dir: p.dir === "asc" ? "desc" : "asc" } : { key, dir: key === "symbol" ? "asc" : "desc" }));
  }

  const sortHeader = (key: SortKey, label: string, left = false) => (
    <th scope="col" className={left ? s.left : undefined} aria-sort={sort.key === key ? (sort.dir === "asc" ? "ascending" : "descending") : undefined}>
      <button type="button" aria-pressed={sort.key === key} aria-label={`Sort by ${label.toLowerCase()}`} onClick={() => sortBy(key)}>
        {label}
        {sort.key === key ? (sort.dir === "asc" ? " ↑" : " ↓") : ""}
      </button>
    </th>
  );

  return (
    <>
      <div className={s.head}>
        <h1>Signals</h1>
        <span className={s.count}>
          {data.total} stocks scored on {fmtDate(data.asOf)}
        </span>
      </div>

      <div className={s.filters}>
        <Segmented
          label="Signal"
          inline
          options={[{ value: "ALL", label: "All" }, ...(["BUY", "HOLD", "SELL"] as const).map((k) => ({ value: k, label: SIGNALS[k].label }))]}
          value={data.filters.signal ?? "ALL"}
          onChange={(v) => go({ signal: isSignalKey(v) ? v : null })}
        />
        <label>
          Sector
          <select autoFocus={focusSector} value={data.filters.sector ?? ""} onChange={(e) => go({ sector: e.target.value || null })}>
            <option value="">All sectors</option>
            {data.sectors.map((x) => (
              <option key={x} value={x}>
                {x}
              </option>
            ))}
          </select>
        </label>
        <label>
          Scored date
          <select value={data.asOf} onChange={(e) => go({ as_of: e.target.value === latest ? null : e.target.value })}>
            {data.dates.map((d) => (
              <option key={d} value={d}>
                {fmtDate(d)}
                {d === latest ? " (latest)" : ""}
              </option>
            ))}
          </select>
        </label>
      </div>

      {data.notice && (
        <Notice tone="warn" className={s.notice}>
          <span>{data.notice}</span>
          {data.fuller && <Link href={`/signals?as_of=${data.fuller.date}`}>See the {fmtDate(data.fuller.date)} list</Link>}
        </Notice>
      )}
      {filtered && (
        <p className={s.showing}>
          Showing {rows.length} of {data.total}
        </p>
      )}

      <Card>
        {rows.length === 0 ? (
          <div className={s.empty}>No stocks match these filters on {fmtDate(data.asOf)}.</div>
        ) : (
          <>
            <div className={`${s.tblWrap} ${s.table}`}>
              <table className={`${s.tbl} tnum`}>
                <thead>
                  <tr>
                    {sortHeader("symbol", "Stock", true)}
                    <th scope="col" className={s.left}>
                      Sector
                    </th>
                    {sortHeader("score", "Score", true)}
                    <th scope="col" className={s.left}>
                      Signal
                    </th>
                    {sortHeader("coverage", "Coverage")}
                    <th scope="col" title="Triggered · ?unchecked">
                      Red flags
                    </th>
                    {sortHeader("delta", data.previousAsOf ? `Change since ${fmtDate(data.previousAsOf)}` : "Change")}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.isin}>
                      <td className={`${s.left} ${s.sym}`}>
                        <Link href={`/stocks/${encodeURIComponent(r.nse_symbol)}`}>{r.nse_symbol}</Link>
                        <span>{r.name}</span>
                      </td>
                      <td className={`${s.left} ${s.sector}`}>{r.sector ?? "—"}</td>
                      <td className={s.left}>
                        {r.composite_score === null ? (
                          <span className={s.nm}>Not scored</span>
                        ) : (
                          <span className={s.scoreCell}>
                            <b>{scoreText(r.composite_score)}</b>
                            <span className={s.bar} aria-hidden="true">
                              <i style={{ width: `${Math.max(0, Math.min(100, r.composite_score))}%` }} />
                            </span>
                          </span>
                        )}
                      </td>
                      <td className={s.left}>
                        <SignalChip signal={isSignalKey(r.signal) ? r.signal : null} />
                      </td>
                      <td data-low-coverage={r.coverage !== null && r.coverage < LOW_COVERAGE ? "" : undefined}>
                        {r.coverage === null ? "—" : pct(r.coverage, 0)}
                      </td>
                      <td>{flagsText(r)}</td>
                      <td>
                        <Change r={r} previousAsOf={data.previousAsOf} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className={s.cards} data-testid="signal-cards">
              {rows.map((r) => (
                <Link key={r.isin} href={`/stocks/${encodeURIComponent(r.nse_symbol)}`} className={s.cardRow}>
                  <div className={s.cardTop}>
                    <b>{r.nse_symbol}</b>
                    <SignalChip signal={isSignalKey(r.signal) ? r.signal : null} />
                  </div>
                  <div className={s.cardName}>{r.name}</div>
                  <div className={`${s.cardMeta} tnum`}>
                    <span>Score {r.composite_score === null ? "n/m" : scoreText(r.composite_score)}</span>
                    <span style={r.coverage !== null && r.coverage < LOW_COVERAGE ? { color: "var(--hold)" } : undefined}>
                      Coverage {r.coverage === null ? "—" : pct(r.coverage, 0)}
                    </span>
                    <span>Flags {flagsText(r)}</span>
                    {r.delta !== null && (
                      <span>
                        Change <Change r={r} previousAsOf={data.previousAsOf} />
                      </span>
                    )}
                  </div>
                </Link>
              ))}
            </div>
          </>
        )}
      </Card>
      <p className="asof" style={{ marginTop: 10 }}>
        Score is a percentile rank within the stocks scored that day. Coverage is the share of the model&apos;s weight backed by data. Red flags: triggered · ?unchecked. † scored by a
        different model version on the earlier date.
      </p>
    </>
  );
}
