import Link from "next/link";
import { SIGNALS } from "@/config/signals";
import { UNIVERSE } from "@/config/site";
import s from "./shell.module.css";

const LABELS = [SIGNALS.BUY, SIGNALS.HOLD, SIGNALS.SELL].map((x) => x.label).join(" / ");

/** Spec §6.6, verbatim. On every page. */
export function Disclaimer() {
  return (
    <footer className={s.disc}>
      <p>
        <b>Not investment advice.</b> Scores and {LABELS} labels are outputs of a quantitative model that ranks{" "}
        {UNIVERSE} stocks against each other on a given date. They are not a recommendation to buy or sell any security
        and don&apos;t account for your circumstances. Data can be incomplete or late; check filings before acting.{" "}
        <i>[Final wording pending legal review.]</i>
      </p>
      <p>
        <Link href="/disclaimer">Full disclaimer</Link>
      </p>
    </footer>
  );
}
