"use client";

import { useRouter } from "next/navigation";
import { useRef, useState, type FormEvent } from "react";
import { Card } from "@/components/ui/Card";
import { Notice } from "@/components/ui/Notice";
import { Segmented } from "@/components/ui/Segmented";
import type { ImportResult } from "@/lib/api/types";
import s from "./portfolio.module.css";

type Mode = "replace" | "add";

const MODES: { value: Mode; label: string; sub: string }[] = [
  { value: "replace", label: "Replace my portfolio", sub: "The file becomes your whole portfolio" },
  { value: "add", label: "Add to my portfolio", sub: "Keep what's saved and add this file's holdings" },
];
const UNREACHABLE = "Couldn't reach the server. Nothing was changed.";
const NO_DEFAULT = "Couldn't check for a saved portfolio. Choose how to save this file.";

type Problem = { message: string; problems: string[] };

async function problemFrom(res: Response): Promise<Problem> {
  try {
    const { detail } = (await res.json()) as { detail?: unknown };
    if (typeof detail === "string") return { message: detail, problems: [] };
    if (detail && typeof detail === "object") {
      const d = detail as { message?: unknown; problems?: unknown };
      if (typeof d.message === "string") {
        const problems = Array.isArray(d.problems) ? d.problems.filter((p): p is string => typeof p === "string") : [];
        return { message: d.message, problems };
      }
    }
  } catch {
    /* fall through */
  }
  return { message: "Something went wrong. Nothing was changed.", problems: [] };
}

/**
 * Choose a Kite holdings CSV and save it as the portfolio, or add it to one.
 * `hasHoldings` picks the starting mode (Add when some are saved); null means
 * the page couldn't tell, so no mode is chosen and Save waits for one.
 */
export function ImportForm({ hasHoldings }: { hasHoldings: boolean | null }) {
  const router = useRouter();
  const [mode, setMode] = useState<Mode | null>(hasHoldings === null ? null : hasHoldings ? "add" : "replace");
  const [file, setFile] = useState<File | null>(null);
  const [saving, setSaving] = useState(false);
  const [problem, setProblem] = useState<Problem | null>(null);
  // State updates land after a second click can already have fired.
  const inFlight = useRef(false);

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!file || !mode || inFlight.current) return;
    inFlight.current = true;
    setSaving(true);
    setProblem(null);
    let done = false;
    try {
      const query = new URLSearchParams({ mode, filename: file.name.slice(0, 200) });
      const res = await fetch(`/api/portfolio/import?${query}`, { method: "POST", body: file });
      if (res.status === 401) {
        done = true;
        router.push("/login?next=/portfolio/import");
      } else if (res.ok) {
        const result = (await res.json()) as ImportResult;
        done = true;
        router.push(`/portfolio?saved=${result.id}`);
      } else {
        setProblem(await problemFrom(res));
      }
    } catch {
      setProblem({ message: UNREACHABLE, problems: [] });
    } finally {
      // After a redirect the button stays off until the page changes.
      if (!done) {
        inFlight.current = false;
        setSaving(false);
      }
    }
  }

  const current = MODES.find((m) => m.value === mode);
  return (
    <Card className={s.importCard}>
      <h1>Import from Zerodha</h1>
      <form className={s.importForm} onSubmit={save}>
        <label className={s.drop}>
          <span className={s.dropTitle}>Drop your Kite holdings file here, or choose a file</span>
          <span className={s.dropHelp}>In Kite: Holdings → Download (.csv)</span>
          <input
            className={s.fileInput}
            type="file"
            accept=".csv,text/csv"
            disabled={saving}
            onChange={(e) => {
              setFile(e.target.files?.[0] ?? null);
              setProblem(null);
            }}
          />
          {file && <span className={s.fileName}>{file.name}</span>}
        </label>
        <Segmented label="How to save" options={MODES} value={mode} onChange={setMode} />
        <p className={s.modeSub}>{current ? current.sub : NO_DEFAULT}</p>
        <button type="submit" className={s.save} disabled={!file || !mode || saving}>
          {saving ? "Saving…" : "Save portfolio"}
        </button>
      </form>
      {problem && (
        <Notice tone="alert">
          <p className={s.noticeText}>{problem.message}</p>
          {problem.problems.length > 0 && (
            <ul className={s.problems}>
              {problem.problems.map((p, i) => (
                <li key={i}>{p}</li>
              ))}
            </ul>
          )}
        </Notice>
      )}
    </Card>
  );
}
