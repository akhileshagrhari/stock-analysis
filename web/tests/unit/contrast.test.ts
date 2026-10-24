import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// WCAG AA for body text (4.5:1) on every text/background pair the tokens
// are used for. Dates (.asof) and the disclaimer use --ink3, so it must pass
// on the page, card and muted backgrounds.
const css = readFileSync(join(process.cwd(), "src", "app", "globals.css"), "utf8");
const token = (name: string): string => {
  const m = css.match(new RegExp(`--${name}:\\s*(#[0-9A-Fa-f]{6})`));
  if (!m) throw new Error(`no --${name}`);
  return m[1];
};

function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const PAIRS: [text: string, background: string][] = [
  ...["ink", "ink2", "ink3", "accent-ink"].flatMap((t) => ["card", "bg", "muted", "muted2"].map((b) => [t, b] as [string, string])),
  ["pos", "pos-soft"],
  ["neg", "neg-soft"],
  ["hold-ink", "hold-soft"],
  ["accent-ink", "accent-soft"],
];

describe("text contrast", () => {
  it.each(PAIRS)("--%s on --%s is at least 4.5:1", (text, background) => {
    expect(contrast(token(text), token(background))).toBeGreaterThanOrEqual(4.5);
  });
});
