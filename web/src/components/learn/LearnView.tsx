import type { ModelInfo } from "@/lib/api/types";
import { fmtDate } from "@/lib/format";
import { FAMILY_MEANING } from "@/config/families";
import { LOW_COVERAGE, UNIVERSE } from "@/config/site";
import { SIGNALS } from "@/config/signals";
import { Card } from "@/components/ui/Card";
import s from "./learn.module.css";

const cap = (f: string) => f.charAt(0).toUpperCase() + f.slice(1);

/** How a score is built, from the live model settings. */
export function LearnView({ model }: { model: ModelInfo }) {
  const max = Math.max(...Object.values(model.family_weights));
  return (
    <div className={s.page}>
      <h1>How scores work</h1>
      <p className={s.lead}>
        Our model ranks {UNIVERSE} companies against each other on each scored date.{" "}
        <strong>Scores are percentiles within the scored universe on each date, not valuations.</strong>
      </p>
      <div className={s.stack}>
        <Card>
          <h2 id="model">Model &amp; weights</h2>
          <p>
            Each company is measured on factors grouped into five families. Each factor becomes a z-score against its
            sector; a family&apos;s z-score is the average of its factors, and the composite is the weighted sum, turned into a
            0–100 percentile across every company scored that day.
          </p>
          <table className={s.tbl} aria-label="Family weights">
            <thead>
              <tr>
                <th scope="col">Family</th>
                <th scope="col">Measures</th>
                <th scope="col" style={{ width: "30%" }}>
                  <span className="srOnly">Weight bar</span>
                </th>
                <th scope="col" className={s.num}>
                  Weight
                </th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(model.family_weights).map(([family, w]) => (
                <tr key={family}>
                  <td>{cap(family)}</td>
                  <td>{FAMILY_MEANING[family] ?? "—"}</td>
                  <td>
                    <i className={s.bar} style={{ width: `${(w / max) * 100}%` }} aria-hidden="true" />
                  </td>
                  <td className={`${s.num} tnum`}>{Math.round(w * 100)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>

        <Card>
          <h2>From score to signal</h2>
          <ul>
            <li>
              {SIGNALS.BUY.label} from {model.buy_threshold}, {SIGNALS.HOLD.label} from {model.sell_threshold} to below{" "}
              {model.buy_threshold}, {SIGNALS.SELL.label} below {model.sell_threshold}.
            </li>
            <li>A triggered red flag forces {SIGNALS.SELL.label}, whatever the score. The page shows the score it would otherwise have had.</li>
            <li>
              Because the score is a rank, a company&apos;s score can move when other companies change, even if its own numbers
              don&apos;t.
            </li>
          </ul>
        </Card>

        <Card>
          <h2>Reading the numbers</h2>
          <ul>
            <li>
              <b>z-scores are sign-adjusted:</b> positive is always good for the company. For debt/equity, a positive z means
              less debt than the sector. Each factor says what a positive value means.
            </li>
            <li>
              <b>Coverage</b> is the share of the model&apos;s factors that could be measured. Below {Math.round(LOW_COVERAGE * 100)}% the
              site says to treat the score with caution; families with nothing measured show as n/m and the others are
              re-weighted.
            </li>
            <li>
              <b>Unchecked is not clean.</b> A red flag the data can&apos;t evaluate shows as “?”, never as passed.
            </li>
            <li>
              <b>Model versions.</b> When the model changes, scores either side of the change are not strictly comparable;
              the history marks each change.
            </li>
          </ul>
        </Card>

        <Card>
          <h2>Scored dates</h2>
          <p>{model.scored_dates.map(fmtDate).join(" · ") || "None yet."}</p>
        </Card>
      </div>
    </div>
  );
}
