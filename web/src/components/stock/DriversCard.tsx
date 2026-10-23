"use client";

import { useState } from "react";
import type { Driver } from "@/lib/api/types";
import { zLabel } from "@/lib/format";
import { formatFactorRaw } from "@/config/factors";
import { Card } from "@/components/ui/Card";
import { Segmented } from "@/components/ui/Segmented";
import s from "./lower.module.css";

const SHOWN = 4;

/**
 * The factors behind the score. z is sign-adjusted (positive is always good
 * for the company), and `meaning` describes a positive z, so it reads
 * "Positive = …" in both lists.
 */
export function DriversCard({ strengths, weaknesses }: { strengths: Driver[]; weaknesses: Driver[] }) {
  const [side, setSide] = useState<"up" | "down">("up");
  const [all, setAll] = useState(false);
  const list = (side === "up" ? strengths.filter((d) => d.z !== null && d.z > 0) : weaknesses.filter((d) => d.z !== null && d.z < 0));
  const shown = all ? list : list.slice(0, SHOWN);
  return (
    <Card>
      <Segmented
        label="Factor direction"
        options={[
          { value: "up", label: "Pulling up" },
          { value: "down", label: "Pulling down" },
        ]}
        value={side}
        onChange={(v) => {
          setSide(v);
          setAll(false);
        }}
      />
      <div className={s.list}>
        {shown.length === 0 && <div className={s.emptyLine}>No factor {side === "up" ? "lifts" : "drags on"} the score.</div>}
        {shown.map((d) => (
          <div key={d.name} className={s.item}>
            <div className={s.t1}>
              <span className={`${s.zp} tnum`} data-tone={d.z! > 0 ? "up" : "down"}>
                {zLabel(d.z!)}
              </span>
              <span>{d.family}</span>
            </div>
            <div className={s.t2}>
              {d.label}
              <span className={`${s.raw} tnum`}>{formatFactorRaw(d.name, d.raw_value)}</span>
            </div>
            {d.meaning && <div className={s.t3}>Positive = {d.meaning}</div>}
          </div>
        ))}
      </div>
      {list.length > SHOWN && (
        <button type="button" className={s.more} onClick={() => setAll((v) => !v)}>
          {all ? "Show fewer" : `Show all ${list.length}`}
        </button>
      )}
    </Card>
  );
}
