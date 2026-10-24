import { describe, expect, it } from "vitest";
import { changeSinceFirst, compare, modelChangeIndexes, snapshots } from "@/lib/stock/history";
import { fixtureStock } from "../fixtures";

describe("history", () => {
  const s = snapshots(fixtureStock("RELIANCE").history);

  it("orders snapshots oldest first", () => {
    expect(s.map((x) => x.date)).toEqual(["2026-07-31", "2026-08-14", "2026-08-16"]);
    expect(snapshots([...fixtureStock("RELIANCE").history].reverse()).map((x) => x.date)).toEqual(s.map((x) => x.date));
  });

  it("marks where the model changed", () => {
    expect(modelChangeIndexes(s)).toEqual([2]);
  });

  it("measures change since the first snapshot", () => {
    expect(changeSinceFirst(s)!.toFixed(1)).toBe("-4.1");
    expect(changeSinceFirst(snapshots(fixtureStock("ADANIENSOL").history))).toBeNull();
    expect(changeSinceFirst([])).toBeNull();
  });

  it("says when two snapshots came from different models", () => {
    expect(compare(s[0], s[2])).toMatchObject({ comparable: false, signalChanged: false });
    expect(compare(s[0], s[2]).scoreDelta!.toFixed(1)).toBe("-4.1");
    expect(compare(s[0], s[1]).comparable).toBe(true);
  });

  it("marks snapshots from partial runs with the number of companies scored", () => {
    const abb = fixtureStock("ABB");
    const r = snapshots(abb.history, abb.runs);
    expect(r.map((x) => [x.date, x.partial, x.runSize])).toEqual([
      ["2026-07-31", false, 77],
      ["2026-08-14", false, 77],
      ["2026-08-15", true, 1],
      ["2026-08-16", true, 4],
    ]);
  });

  it("a partial run on either side is not comparable, even under the same model", () => {
    const abb = fixtureStock("ABB");
    const r = snapshots(abb.history, abb.runs);
    expect(compare(r[2], r[3])).toMatchObject({ comparable: false, modelChanged: false });
    expect(compare(r[0], r[1])).toMatchObject({ comparable: true, modelChanged: false });
  });

  it("without run sizes nothing is marked partial", () => {
    expect(s.every((x) => x.partial === false && x.runSize === null)).toBe(true);
  });

  it("gives no delta when either score is missing", () => {
    expect(compare({ ...s[0], score: null }, s[2]).scoreDelta).toBeNull();
  });
});
