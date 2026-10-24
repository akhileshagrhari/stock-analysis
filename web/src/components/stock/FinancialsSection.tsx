"use client";

import { useState } from "react";
import type { Financials, Quarter } from "@/lib/api/types";
import { fiscalLabel, fmtDay, num, pct, quarterLabel } from "@/lib/format";
import { Card } from "@/components/ui/Card";
import { Segmented } from "@/components/ui/Segmented";
import s from "./lower.module.css";

const NA = (
  <span className={s.na} title="Not in the filing">
    —
  </span>
);

function filed(iso: string | null): string {
  return iso ? `${fmtDay(iso)} '${iso.slice(2, 4)}` : "—";
}

// Filings can switch between consolidated and standalone from one period to
// the next (ABB: FY25 consolidated, FY24 standalone), so the basis is given
// per column, never once for the section.
function quarterBasis(q: Quarter): string {
  return q.is_consolidated === true ? "Consolidated" : q.is_consolidated === false ? "Standalone" : "—";
}

function annualBasis(basis: string | null): string {
  return basis ? basis.charAt(0).toUpperCase() + basis.slice(1).toLowerCase() : "—";
}

function MixedNote({ bases }: { bases: string[] }) {
  if (new Set(bases.filter((b) => b !== "—")).size < 2) return null;
  return <p className={s.foot}>These columns mix consolidated and standalone figures, so they don&apos;t compare like for like.</p>;
}

function margin(q: Quarter): number | null {
  return q.pat === null || q.revenue === null || q.revenue === 0 ? null : q.pat / q.revenue;
}

/** Quarterly and annual results as filed; each column dated by its filing. */
export function FinancialsSection({ financials }: { financials: Financials }) {
  const [view, setView] = useState<"q" | "a">("q");
  const q = financials.quarterly;
  const a = financials.annual;
  const cell = (v: number | null, digits = 0) => (v === null ? NA : num(v, digits));

  return (
    <section id="financials" className={s.block} style={{ scrollMarginTop: 64 }} aria-labelledby="financials-h">
      <h2 className={s.secH} id="financials-h">
        Financials
      </h2>
      <Card>
        <div className={s.finHead}>
          <Segmented
            label="Financials period"
            inline
            options={[
              { value: "q", label: "Quarterly" },
              { value: "a", label: "Annual" },
            ]}
            value={view}
            onChange={setView}
          />
          <span className="asof">₹ crore (EPS in ₹) · basis and filing date under each column</span>
        </div>

        {view === "q" &&
          (q.length === 0 ? (
            <div className={s.emptyLine}>No quarterly results stored.</div>
          ) : (
            <>
              <div className={s.tblWrap}>
                <table className={`${s.tbl} ${s.fin} tnum`}>
                  <thead>
                    <tr>
                      <th scope="col">
                        <span className="srOnly">Line</span>
                      </th>
                      {q.map((x) => (
                        <th key={x.period_end} scope="col">
                          {quarterLabel(x.period_end)}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <td>Revenue</td>
                      {q.map((x) => (
                        <td key={x.period_end}>{cell(x.revenue)}</td>
                      ))}
                    </tr>
                    <tr>
                      <td>Net profit (PAT)</td>
                      {q.map((x) => (
                        <td key={x.period_end}>{cell(x.pat)}</td>
                      ))}
                    </tr>
                    <tr>
                      <td>PAT margin</td>
                      {q.map((x) => {
                        const m = margin(x);
                        return <td key={x.period_end}>{m === null ? NA : pct(m, 1)}</td>;
                      })}
                    </tr>
                    <tr>
                      <td>EPS (₹)</td>
                      {q.map((x) => (
                        <td key={x.period_end}>{cell(x.eps, 2)}</td>
                      ))}
                    </tr>
                    <tr className={s.filed}>
                      <td>Basis</td>
                      {q.map((x) => (
                        <td key={x.period_end}>{quarterBasis(x)}</td>
                      ))}
                    </tr>
                    <tr className={s.filed}>
                      <td>Filed</td>
                      {q.map((x) => (
                        <td key={x.period_end}>{filed(x.filing_date)}</td>
                      ))}
                    </tr>
                  </tbody>
                </table>
              </div>
              <MixedNote bases={q.map(quarterBasis)} />
            </>
          ))}

        {view === "a" &&
          (a.length === 0 ? (
            <div className={s.emptyLine}>No annual figures in this company&apos;s filings</div>
          ) : (
            <>
              <div className={s.tblWrap}>
                <table className={`${s.tbl} ${s.fin} tnum`}>
                  <thead>
                    <tr>
                      <th scope="col">
                        <span className="srOnly">Line</span>
                      </th>
                      {a.map((x) => (
                        <th key={x.fiscal_year} scope="col">
                          {fiscalLabel(x.fiscal_year, x.period_end)}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {financials.line_items.map((li) => (
                      <tr key={li.key}>
                        <td>{li.label}</td>
                        {a.map((x) => (
                          <td key={x.fiscal_year}>{cell(x.values[li.key] ?? null, li.per_share ? 2 : 0)}</td>
                        ))}
                      </tr>
                    ))}
                    <tr>
                      <td>Debt / equity</td>
                      {a.map((x) => {
                        const d = x.values.total_debt ?? null;
                        const e = x.values.total_equity ?? null;
                        return <td key={x.fiscal_year}>{d === null || e === null || e <= 0 ? NA : num(d / e, 2)}</td>;
                      })}
                    </tr>
                    <tr className={s.filed}>
                      <td>Basis</td>
                      {a.map((x) => (
                        <td key={x.fiscal_year}>{annualBasis(x.basis)}</td>
                      ))}
                    </tr>
                    <tr className={s.filed}>
                      <td>Filed · auditor</td>
                      {a.map((x) => (
                        <td key={x.fiscal_year}>
                          {filed(x.filing_date)}
                          {x.auditor_opinion && ` · ${x.auditor_opinion === "UNMODIFIED" ? "clean" : x.auditor_opinion.toLowerCase()}`}
                        </td>
                      ))}
                    </tr>
                  </tbody>
                </table>
              </div>
              <MixedNote bases={a.map((x) => annualBasis(x.basis))} />
            </>
          ))}
        <p className={s.foot}>EPS is as filed and is not adjusted for later bonus issues or splits.</p>
      </Card>
    </section>
  );
}
