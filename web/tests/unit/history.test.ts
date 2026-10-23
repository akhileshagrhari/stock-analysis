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

  it("gives no delta when either score is missing", () => {
    expect(compare({ ...s[0], score: null }, s[2]).scoreDelta).toBeNull();
  });
});
