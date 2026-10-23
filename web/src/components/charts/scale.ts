/** A "nice" step (1, 2, 2.5, 5 × 10^n) giving about `ticks` intervals over [lo, hi]. */
export function niceStep(lo: number, hi: number, ticks = 4): number {
  const range = (hi - lo) / ticks;
  if (!(range > 0) || !Number.isFinite(range)) return Math.max(Math.abs(hi) * 0.01, 1);
  const p = 10 ** Math.floor(Math.log10(range));
  return [1, 2, 2.5, 5, 10].map((m) => m * p).find((step) => step >= range) ?? 10 * p;
}

/** [lo, hi] widened to whole steps; a flat series still gets a visible range. */
export function niceDomain(values: number[], ticks = 4): { lo: number; hi: number; step: number } {
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  if (lo === hi) {
    const pad = Math.max(Math.abs(lo) * 0.01, 1);
    lo -= pad;
    hi += pad;
  }
  const step = niceStep(lo, hi, ticks);
  return { lo: Math.floor(lo / step) * step, hi: Math.ceil(hi / step) * step, step };
}
