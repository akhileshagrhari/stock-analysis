import { NOT_SCORED, SIGNALS, type SignalKey } from "@/config/signals";
import s from "./ui.module.css";

/** A signal with its colour, always with the label as text. */
export function SignalChip({ signal }: { signal: SignalKey | null }) {
  if (signal === null) {
    return <span className={`${s.sig} ${s.none}`}>{NOT_SCORED}</span>;
  }
  const { label, tone, meaning } = SIGNALS[signal];
  return (
    <span className={`${s.sig} ${s[tone]}`} title={meaning}>
      <span className={s.d} aria-hidden="true" />
      {label}
    </span>
  );
}
