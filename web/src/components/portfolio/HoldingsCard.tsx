import Link from "next/link";
import type { GroupShare, Holding } from "@/lib/api/types";
import { count, fmtDate, inr, num, pct, scoreText } from "@/lib/format";
import { flagStates } from "@/config/redflags";
import { isSignalKey, NOT_RATED, SIGNALS } from "@/config/signals";
import { Icon } from "@/components/shell/icons";
import { Card } from "@/components/ui/Card";
import { Pill } from "@/components/ui/Pill";
import { SignalChip } from "@/components/ui/SignalChip";
import s from "./portfolio.module.css";

type Group = Holding["group"];

export const GROUP_ORDER: Group[] = ["BUY", "HOLD", "SELL", "NOT_RATED"];

export function groupLabel(group: Group): string {
  return group === "NOT_RATED" ? NOT_RATED : SIGNALS[group].label;
}

const DASH = "—";

/** Whole shares as they are; a fraction (rare, after some corporate actions) to at most four places. */
function qty(q: number): string {
  return Number.isInteger(q) ? num(q) : num(q, 4).replace(/\.?0+$/, "");
}

function Pills({ h }: { h: Holding }) {
  const flags = flagStates(h.red_flags, []).filter((f) => f.state === "triggered");
  const was = isSignalKey(h.was_signal) ? h.was_signal : null;
  const rated = h.older_rating && h.signal_as_of ? `Rated ${fmtDate(h.signal_as_of)}${h.earlier_model ? ", earlier model" : ""}` : null;
  if (flags.length === 0 && !was && !rated && !h.split_since_upload) return null;
  return (
    <div className={s.pills}>
      {flags.map((f) => (
        <Pill key={f.name} tone="neg" title={f.label}>
          <Icon name="flag" size={12} />
          <span className="srOnly">Red flag: </span>
          {f.short}
        </Pill>
      ))}
      {was && <Pill>{`was ${SIGNALS[was].label}`}</Pill>}
      {rated && <Pill>{rated}</Pill>}
      {h.split_since_upload && <Pill tone="hold">Split or bonus since your upload. Import again to update</Pill>}
    </div>
  );
}

function Row({ h }: { h: Holding }) {
  const move = h.pnl_pct === null ? "" : h.pnl_pct > 0 ? s.up : h.pnl_pct < 0 ? s.down : "";
  return (
    <tr className={s.row}>
      <td className={s.stock}>
        <div className={s.id}>
          {h.isin ? (
            <Link href={`/stocks/${encodeURIComponent(h.symbol)}`} className={s.sym}>
              {h.symbol}
            </Link>
          ) : (
            <b className={s.sym}>{h.symbol}</b>
          )}
          {h.name && <span className={s.name}>{h.name}</span>}
        </div>
        {h.not_rated_reason && <div className={s.reason}>{h.not_rated_reason}</div>}
        <Pills h={h} />
      </td>
      <td className={s.qty}>
        <span className={s.lab} aria-hidden="true">
          Qty{" "}
        </span>
        {qty(h.quantity)}
      </td>
      <td className={s.avg}>
        <span className={s.lab} aria-hidden="true">
          Avg cost{" "}
        </span>
        {num(h.avg_cost, 2)}
      </td>
      <td className={s.value}>{h.value === null ? DASH : inr(h.value)}</td>
      <td className={s.weight}>{h.weight === null ? DASH : pct(h.weight)}</td>
      <td className={`${s.pnl} ${move}`}>{h.pnl_pct === null ? DASH : pct(h.pnl_pct, 1, true)}</td>
      <td className={s.score}>
        <span className={s.lab} aria-hidden="true">
          Score{" "}
        </span>
        {h.score === null ? DASH : scoreText(h.score)}
      </td>
    </tr>
  );
}

/**
 * Every holding, one section per rating that has any, in BUY, HOLD, SELL,
 * Not rated order; within a section, as the API sorted them (largest value
 * first). On a phone each row reflows to two lines (CSS only).
 */
export function HoldingsCard({ holdings, groups }: { holdings: Holding[]; groups: GroupShare[] }) {
  const share = new Map(groups.map((g) => [g.group, g.share]));
  const sections = GROUP_ORDER.map((group) => ({ group, rows: holdings.filter((h) => h.group === group) })).filter((x) => x.rows.length > 0);
  return (
    <Card className={s.holdings}>
      <div className={s.tblWrap}>
        <table className={`${s.tbl} tnum`}>
          <caption className="srOnly">Holdings by the model&apos;s rating</caption>
          <thead>
            <tr>
              <th scope="col" className={s.stock}>
                Stock
              </th>
              <th scope="col">Qty</th>
              <th scope="col">Avg cost</th>
              <th scope="col">Value</th>
              <th scope="col" className={s.weight}>
                Weight
              </th>
              <th scope="col">P&amp;L</th>
              <th scope="col">Score</th>
            </tr>
          </thead>
          {sections.map(({ group, rows }) => (
            <tbody key={group} data-group={group}>
              <tr className={s.groupRow}>
                <th scope="rowgroup" colSpan={7}>
                  <span className={s.groupHead}>
                    {group === "NOT_RATED" ? <Pill tone="grey">{NOT_RATED}</Pill> : <SignalChip signal={group} />}
                    <span className={s.groupMeta}>{`${count(rows.length, "holding")} · ${pct(share.get(group) ?? 0)}`}</span>
                  </span>
                </th>
              </tr>
              {rows.map((h, i) => (
                <Row key={`${h.symbol}-${h.isin ?? ""}-${i}`} h={h} />
              ))}
            </tbody>
          ))}
        </table>
      </div>
    </Card>
  );
}
