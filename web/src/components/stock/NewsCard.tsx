"use client";

import { useState } from "react";
import type { NewsItem } from "@/lib/api/types";
import { fmtDate, signedFixed } from "@/lib/format";
import { Card } from "@/components/ui/Card";
import { Segmented } from "@/components/ui/Segmented";
import s from "./lower.module.css";

const SHOWN = 5;

function tone(item: NewsItem): { tone: string; text: string } {
  if (item.label === null) return { tone: "none", text: "Not scored yet" };
  const name = item.label.charAt(0).toUpperCase() + item.label.slice(1);
  const t = item.label === "positive" ? "up" : item.label === "negative" ? "down" : "neutral";
  return { tone: t, text: item.score === null ? name : `${name} ${signedFixed(item.score, 2)}` };
}

/** Recent headlines with their sentiment. An unscored item is never shown as neutral. */
export function NewsCard({ news }: { news: NewsItem[] }) {
  const scoredCount = news.filter((n) => n.label !== null).length;
  const [mode, setMode] = useState<"scored" | "all">(scoredCount ? "scored" : "all");
  const [all, setAll] = useState(false);
  const list = mode === "scored" ? news.filter((n) => n.label !== null) : news;
  const shown = all ? list : list.slice(0, SHOWN);
  return (
    <Card>
      <Segmented
        label="News filter"
        options={[
          { value: "scored", label: "Scored" },
          { value: "all", label: `All ${news.length}` },
        ]}
        value={mode}
        onChange={(v) => {
          setMode(v);
          setAll(false);
        }}
      />
      <div className={s.list}>
        {news.length === 0 && <div className={s.emptyLine}>No news stored for this company.</div>}
        {news.length > 0 && list.length === 0 && <div className={s.emptyLine}>None of the latest {news.length} items is scored yet.</div>}
        {shown.map((n, i) => {
          const t = tone(n);
          return (
            <div key={`${n.url ?? n.headline}-${i}`} className={s.item}>
              <div className={s.t1}>
                <span className={`${s.zp} tnum`} data-tone={t.tone}>
                  {t.text}
                </span>
                <span className="tnum">{n.published_at ? fmtDate(n.published_at) : "Undated"}</span>
              </div>
              <div className={s.t2} style={{ fontSize: 14.5 }}>
                {n.url ? (
                  <a href={n.url} target="_blank" rel="noopener noreferrer">
                    {n.headline ?? n.url}
                  </a>
                ) : (
                  n.headline
                )}
              </div>
              {n.source && <div className={s.t3}>{n.source}</div>}
            </div>
          );
        })}
      </div>
      {list.length > SHOWN && (
        <button type="button" className={s.more} onClick={() => setAll((v) => !v)}>
          {all ? "Show fewer" : `Show all ${list.length}`}
        </button>
      )}
    </Card>
  );
}
