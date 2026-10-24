import { signedFixed } from "@/lib/format";
import s from "./charts.module.css";

/** Zero in the middle; positive grows right in green, negative left in red. Null is hatched. */
export function DivergingBar({ value, max }: { value: number | null; max: number }) {
  if (value === null || !Number.isFinite(value)) {
    return (
      <div className={s.cbar} role="img" aria-label="Not measured">
        <span className={s.hatch} data-hatched="" />
      </div>
    );
  }
  const width = `${Math.min(Math.abs(value) / (max > 0 ? max : 1), 1) * 50}%`;
  const pos = value >= 0;
  return (
    <div className={s.cbar} role="img" aria-label={`Contribution ${signedFixed(value, 2)}`} title={signedFixed(value, 2)}>
      <b data-testid="bar" data-sign={pos ? "pos" : "neg"} style={pos ? { left: "50%", width } : { right: "50%", width }} />
    </div>
  );
}
