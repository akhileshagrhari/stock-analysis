import { BRAND, UNIVERSE } from "@/config/site";
import { SIGNALS } from "@/config/signals";
import { Card } from "@/components/ui/Card";
import s from "./learn.module.css";

const LABELS = [SIGNALS.BUY, SIGNALS.HOLD, SIGNALS.SELL].map((x) => x.label).join(" / ");

/** Long-form disclaimer. A placeholder until the owner's legal review. */
export function DisclaimerView() {
  return (
    <div className={s.page}>
      <h1>Disclaimer</h1>
      <p className={s.pending}>Placeholder text, pending legal review. The final wording will replace this page.</p>
      <div className={s.stack} style={{ marginTop: 16 }}>
        <Card>
          <h2>What the scores are</h2>
          <p>
            {BRAND} publishes the output of a quantitative model. The model ranks {UNIVERSE} stocks against each other on a
            given date using reported financials, prices and news. Scores and {LABELS} labels describe that ranking. They are
            not a recommendation to buy, sell or hold any security, and they do not take any reader&apos;s circumstances,
            objectives or risk tolerance into account.
          </p>
        </Card>
        <Card>
          <h2>Data</h2>
          <p>
            Data comes from public filings, exchange prices and news sources. It can be incomplete, late or wrong, and a
            score can rest on a minority of the model&apos;s factors (each page shows its coverage). Every figure carries the
            date it is true as of. Check the filings before acting on anything here.
          </p>
        </Card>
        <Card>
          <h2>No advice</h2>
          <p>
            Nothing on this site is investment advice or research under any regulation. Past scores and past prices do not
            predict future returns. [Final wording pending legal review.]
          </p>
        </Card>
      </div>
    </div>
  );
}
