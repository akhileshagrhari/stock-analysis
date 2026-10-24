// The signal labels, their colours and one-line meanings. Nothing else in the
// codebase writes BUY, HOLD or SELL as display text, so renaming them (the
// owner may move to Good / Average / Bad after legal advice) is this file.

export type SignalKey = "BUY" | "HOLD" | "SELL";
export type Tone = "pos" | "hold" | "neg";

export const SIGNALS: Record<SignalKey, { label: string; tone: Tone; meaning: string }> = {
  BUY: { label: "BUY", tone: "pos", meaning: "Our model ranks it in the top band of scored NIFTY100 stocks." },
  HOLD: { label: "HOLD", tone: "hold", meaning: "Our model ranks it in the middle band." },
  SELL: { label: "SELL", tone: "neg", meaning: "Our model ranks it in the bottom band, or a red flag forced it there." },
};

export const NOT_SCORED = "Not scored";

export function isSignalKey(key: string | null | undefined): key is SignalKey {
  return key === "BUY" || key === "HOLD" || key === "SELL";
}

export function signalLabel(key: string | null): string {
  if (key === null) return NOT_SCORED;
  return isSignalKey(key) ? SIGNALS[key].label : key;
}
