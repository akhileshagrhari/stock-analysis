import Link from "next/link";
import { fmtDate } from "@/lib/format";
import s from "./ui.module.css";

/**
 * What a score or signal is, said beside it rather than only at the page
 * foot: a model rank on a date, not investment advice (the disclaimer's own
 * words), with the explainer and the full disclaimer one tap away.
 */
export function VerdictNote({ date, many = false, className }: { date: string | null; many?: boolean; className?: string }) {
  return (
    <p className={className ? `${s.verdictNote} ${className}` : s.verdictNote}>
      <span>
        Model {many ? "ranks" : "rank"}
        {date ? ` on ${fmtDate(date)}` : ""}, not investment advice.
      </span>{" "}
      <Link href="/learn">How to read this</Link> · <Link href="/disclaimer">Disclaimer</Link>
    </p>
  );
}
