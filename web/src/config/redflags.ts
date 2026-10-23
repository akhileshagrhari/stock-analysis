// The model's six red flags, copied from
// src/stockanalysis/factors/redflags.py::DEFINITIONS until /v1/model serves
// them (API requirement R3). Two have no data source, so they are always
// "couldn't be checked" — which is not the same as clear.

export const RED_FLAGS: { name: string; description: string; reachable: boolean }[] = [
  { name: "promoter_pledge", description: "Promoter pledge > 25% of promoter holding", reachable: false },
  { name: "auditor_qualification", description: "Auditor qualification, adverse opinion or disclaimer", reachable: true },
  { name: "weak_cash_conversion", description: "CFO/PAT < 0.5 for 3 consecutive years", reachable: true },
  { name: "promoter_selling", description: "Promoter holding falling 3 consecutive quarters", reachable: true },
  { name: "contingent_liabilities", description: "Contingent liabilities > 50% of net worth", reachable: true },
  { name: "rating_downgrade", description: "Any credit rating downgrade", reachable: false },
];

export type FlagState = { name: string; label: string; state: "clear" | "triggered" | "unknown" };

type Triggered = { name: string; description?: string | null }[] | string[];

/** Every catalogued flag with its state, then any flag the catalogue lacks. */
export function flagStates(red: Triggered, unknown: string[]): FlagState[] {
  const triggered = new Map<string, string | null>();
  for (const f of red) {
    if (typeof f === "string") triggered.set(f, null);
    else triggered.set(f.name, f.description ?? null);
  }
  const unknownSet = new Set(unknown);
  const known = new Set(RED_FLAGS.map((f) => f.name));

  const out: FlagState[] = RED_FLAGS.map((f) => ({
    name: f.name,
    label: triggered.get(f.name) ?? f.description,
    state: triggered.has(f.name) ? "triggered" : unknownSet.has(f.name) ? "unknown" : "clear",
  }));
  for (const [name, description] of triggered) {
    if (!known.has(name)) out.push({ name, label: description ?? humanise(name), state: "triggered" });
  }
  for (const name of unknown) {
    if (!known.has(name) && !triggered.has(name)) out.push({ name, label: humanise(name), state: "unknown" });
  }
  return out;
}

function humanise(name: string): string {
  const s = name.replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}
