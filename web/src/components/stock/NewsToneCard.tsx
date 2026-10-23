import type { Family, NewsMix } from "@/lib/api/types";
import { newsTone } from "@/lib/stock/state";
import { HalfGauge } from "@/components/charts/HalfGauge";
import { Card } from "@/components/ui/Card";
import s from "./stock.module.css";

/** The 30-day news mix. No scored news is not the same as neutral news. */
export function NewsToneCard({ news, families }: { news: NewsMix | null; families: Family[] }) {
  const tone = newsTone(news);
  const sentiment = families.find((f) => f.family === "sentiment");
  const notMeasured = sentiment !== undefined && sentiment.factors_measured === 0;
  return (
    <Card className={s.gauge}>
      <div className={s.row}>
        News tone <span className={s.cnt}>{news ? `${news.total} · 30d` : "30d"}</span>
      </div>
      <HalfGauge value={tone.needle} label={tone.label ? `News tone: ${tone.label}` : "No scored news"} />
      <div className={s.gval}>{tone.label ?? "—"}</div>
      <div className={s.glab}>{tone.counts}</div>
      {notMeasured && (
        <div className="asof" style={{ marginTop: 6 }}>
          Too few articles to score sentiment
        </div>
      )}
    </Card>
  );
}
