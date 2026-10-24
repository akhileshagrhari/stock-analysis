/** Which x positions get a label: always the ends, then any middle one at least `gap` from its labelled neighbours. */
export function labelIndexes(xs: number[], gap: number): number[] {
  if (xs.length <= 1) return xs.map((_, i) => i);
  const last = xs.length - 1;
  const out = [0];
  for (let i = 1; i < last; i++) {
    if (xs[i] - xs[out[out.length - 1]] >= gap && xs[last] - xs[i] >= gap) out.push(i);
  }
  out.push(last);
  return out;
}
