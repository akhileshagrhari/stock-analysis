// A half-circle of 14 segments, red to grey to green, with a needle at value
// (0 = all negative, 1 = all positive). Null draws no needle.
const CX = 70;
const CY = 74;
const R = 58;
const N = 14;

function point(a: number, r = R): [number, number] {
  return [CX + r * Math.cos(Math.PI * (1 - a)), CY - r * Math.sin(Math.PI * (1 - a))];
}

export function HalfGauge({ value, label }: { value: number | null; label?: string }) {
  const has = value !== null && Number.isFinite(value);
  return (
    <svg viewBox="0 0 140 84" width="150" height="90" role="img" aria-label={label ?? (has ? `Tone ${Math.round(value * 100)} of 100` : "No scored news")}>
      {Array.from({ length: N }, (_, i) => {
        const [x0, y0] = point(i / N + 0.012);
        const [x1, y1] = point((i + 1) / N - 0.012);
        const t = i / (N - 1);
        const stroke = t < 0.4 ? "var(--zone-sell-soft)" : t < 0.6 ? "var(--zone-neutral)" : "var(--zone-buy-soft)";
        return <path key={i} d={`M${x0},${y0} A${R},${R} 0 0 1 ${x1},${y1}`} fill="none" stroke={stroke} strokeWidth={9} strokeLinecap="round" />;
      })}
      {has &&
        (() => {
          const [x, y] = point(Math.max(0, Math.min(1, value)), R * 0.78);
          return (
            <g data-testid="needle">
              <line x1={CX} y1={CY} x2={x} y2={y} stroke="var(--ink)" strokeWidth={2} strokeLinecap="round" />
              <circle cx={CX} cy={CY} r={4} fill="var(--ink)" />
            </g>
          );
        })()}
    </svg>
  );
}
