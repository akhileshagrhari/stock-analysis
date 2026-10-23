import type { RedFlag } from "@/lib/api/types";
import { flagStates } from "@/config/redflags";
import { Card } from "@/components/ui/Card";
import s from "./stock.module.css";

const MARK = { clear: "✓", triggered: "✗", unknown: "?" } as const;
const STATE_TEXT = { clear: "Checked: no flag", triggered: "Triggered", unknown: "Couldn't be checked: data missing" } as const;

/** Every flag the model has: clear, triggered, or couldn't be checked. Unchecked is never shown as clear. */
export function RedFlagsCard({ red, unknown }: { red: RedFlag[]; unknown: string[] }) {
  const states = flagStates(red, unknown);
  const triggered = states.filter((f) => f.state === "triggered").length;
  const unchecked = states.filter((f) => f.state === "unknown").length;
  return (
    <Card className={s.gauge} data-testid="redflags-card">
      <div className={s.row}>
        Red flags <span className={s.cnt}>{unchecked} unchecked</span>
      </div>
      <div className={s.gval} style={{ marginTop: 12, color: triggered ? "var(--neg)" : undefined }} data-testid="flags-triggered">
        {triggered}
      </div>
      <div className={s.glab}>triggered</div>
      <div className={s.flags}>
        {states.map((f) => (
          <span key={f.name} data-state={f.state} title={`${f.label}. ${STATE_TEXT[f.state]}.`}>
            {MARK[f.state]} {f.short}
          </span>
        ))}
      </div>
      <div className="asof">Unchecked ≠ clean</div>
    </Card>
  );
}
