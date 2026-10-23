// Formatting for every figure on the site. Negative numbers use the true minus
// sign (U+2212), digits are grouped the Indian way, and dates are parsed from
// their parts: `new Date("2025-08-14")` is UTC midnight, which in IST is
// already the 14th at 05:30 but becomes the 13th when shifted back with
// toISOString(). That bug shipped in the mock; it doesn't ship here.

export const MINUS = "−";
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function parts(iso: string): [number, number, number] {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return [y, m, d];
}

/** Local midnight of a YYYY-MM-DD day. */
export function parseDay(iso: string): Date {
  const [y, m, d] = parts(iso);
  return new Date(y, m - 1, d);
}

/** YYYY-MM-DD of a local date, the inverse of parseDay. */
export function isoDay(d: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

export function fmtDate(iso: string): string {
  const [y, m, d] = parts(iso);
  return `${d} ${MONTHS[m - 1]} ${y}`;
}

export function fmtDay(iso: string): string {
  const [, m, d] = parts(iso);
  return `${d} ${MONTHS[m - 1]}`;
}

/** Whole days from a to b (positive when b is later). */
export function daysBetween(aIso: string, bIso: string): number {
  const [ay, am, ad] = parts(aIso);
  const [by, bm, bd] = parts(bIso);
  return Math.round((Date.UTC(by, bm - 1, bd) - Date.UTC(ay, am - 1, ad)) / 86_400_000);
}

export function quarterLabel(periodEnd: string): string {
  const [y, m] = parts(periodEnd);
  return `${MONTHS[m - 1]} '${String(y).slice(2)}`;
}

/** FY26 for a March year-end; FY25 (Dec) for anything else. */
export function fiscalLabel(fiscalYear: number, periodEnd: string | null): string {
  const label = `FY${String(fiscalYear).slice(2)}`;
  if (!periodEnd) return label;
  const [, m] = parts(periodEnd);
  return m === 3 ? label : `${label} (${MONTHS[m - 1]})`;
}

function signed(text: string, v: number, withPlus: boolean): string {
  if (v < 0) return MINUS + text;
  return withPlus && v > 0 ? `+${text}` : text;
}

const grouping = new Map<number, Intl.NumberFormat>();
function grouped(abs: number, digits: number): string {
  let f = grouping.get(digits);
  if (!f) {
    f = new Intl.NumberFormat("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
    grouping.set(digits, f);
  }
  return f.format(abs);
}

/** 10,75,675 */
export function num(v: number, digits = 0): string {
  return signed(grouped(Math.abs(roundTo(v, digits)), digits), roundTo(v, digits), false);
}

/** ₹1,310.00 */
export function inr(v: number, digits = 2): string {
  return signed(`₹${grouped(Math.abs(roundTo(v, digits)), digits)}`, roundTo(v, digits), false);
}

/** A fraction as a percentage: pct(-0.0464, 2) is −4.64%. */
export function pct(v: number, digits = 1, withPlus = false): string {
  const p = v * 100;
  return signed(`${absFixed(p, digits)}%`, roundTo(p, digits), withPlus);
}

/** Signed fixed-point, for z-scores and deltas: +15.2, −1.4. */
export function signedFixed(v: number, digits = 1): string {
  return signed(absFixed(v, digits), roundTo(v, digits), true);
}

/** Rounds half away from zero on the decimal value, so 7.85 → 7.9 even though
 *  the double nearest 7.85 is slightly below it. */
export function roundTo(v: number, digits: number): number {
  const f = 10 ** digits;
  const scaled = Number((Math.abs(v) * f).toPrecision(12));
  return (Math.sign(v) * Math.round(scaled)) / f;
}

/** toFixed with roundTo's rounding, without a sign. */
export function absFixed(v: number, digits: number): string {
  return Math.abs(roundTo(v, digits)).toFixed(digits);
}

export function ordinal(n: number): string {
  const r = Math.round(n);
  const suffix = r % 100 >= 10 && r % 100 <= 20 ? "th" : ({ 1: "st", 2: "nd", 3: "rd" } as Record<number, string>)[r % 10] ?? "th";
  return `${r}${suffix}`;
}

export function zLabel(z: number): string {
  return `${signedFixed(z, 1)} SD vs sector`;
}

/** The hash at the end of a model version: phase2-composite-4b05e87d → 4b05e87d. */
export function shortModel(version: string): string {
  return version.slice(version.lastIndexOf("-") + 1);
}
