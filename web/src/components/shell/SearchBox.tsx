"use client";

import { useRouter } from "next/navigation";
import { useEffect, useId, useRef, useState, useSyncExternalStore } from "react";
import { browserGet } from "@/lib/api/client";
import type { Instrument } from "@/lib/api/types";
import { UNIVERSE } from "@/config/site";
import { Icon } from "./icons";
import s from "./search.module.css";

const DEBOUNCE_MS = 150;
const noSubscribe = () => () => {};
const LIMIT = 8;

/**
 * Autocomplete over /v1/instruments/search. Blank input never queries (the
 * API rejects it with 422), each query aborts the last, and a response that
 * arrives after a newer one is dropped by request id.
 */
export function SearchBox({ autoFocus = false, size = "hero" }: { autoFocus?: boolean; size?: "hero" | "bar" }) {
  const router = useRouter();
  const listId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const controller = useRef<AbortController | null>(null);
  const reqId = useRef(0);

  const [q, setQ] = useState("");
  const [results, setResults] = useState<Instrument[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  // Platform is only known in the browser; the server renders ⌘K.
  const isMac = useSyncExternalStore(noSubscribe, () => /Mac|iPhone|iPad/.test(navigator.platform), () => true);

  useEffect(() => {
    if (size !== "bar") return;
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        inputRef.current?.focus();
        inputRef.current?.select();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [size]);

  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
      controller.current?.abort();
    },
    [],
  );

  async function run(query: string) {
    const id = ++reqId.current;
    const c = new AbortController();
    controller.current = c;
    try {
      const found = await browserGet<Instrument[]>(`/instruments/search?q=${encodeURIComponent(query)}&limit=${LIMIT}`, c.signal);
      if (id !== reqId.current) return;
      // Search never 404s for "no match" (that's []), so null means no answer.
      setResults(found ?? []);
      setFailed(found === null);
    } catch {
      // A request this box cancelled (the user kept typing) is not an outage.
      if (id !== reqId.current || c.signal.aborted) return;
      setResults([]);
      setFailed(true);
    }
    setActive(-1);
    setOpen(true);
  }

  function onChange(value: string) {
    setQ(value);
    if (timer.current) clearTimeout(timer.current);
    controller.current?.abort();
    const query = value.trim();
    if (!query) {
      reqId.current++;
      setResults(null);
      setOpen(false);
      return;
    }
    timer.current = setTimeout(() => run(query), DEBOUNCE_MS);
  }

  function go(inst: Instrument) {
    setOpen(false);
    router.push(`/stocks/${encodeURIComponent(inst.nse_symbol)}`);
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    const n = results?.length ?? 0;
    if (e.key === "ArrowDown" && n) {
      e.preventDefault();
      setOpen(true);
      setActive((a) => Math.min(a + 1, n - 1));
    } else if (e.key === "ArrowUp" && n) {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
    } else if (e.key === "Enter" && open && results && n) {
      e.preventDefault();
      go(results[active >= 0 ? active : 0]);
    } else if (e.key === "Escape") {
      setOpen(false);
      setActive(-1);
    }
  }

  const showList = open && results !== null;
  const optionId = (i: number) => `${listId}-o${i}`;

  return (
    <div className={`${s.box} ${size === "hero" ? s.hero : ""}`}>
      <label className={s.field}>
        <Icon name="search" />
        <span className="srOnly">Search {UNIVERSE} stocks</span>
        <input
          ref={inputRef}
          role="combobox"
          aria-expanded={showList}
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={showList && active >= 0 ? optionId(active) : undefined}
          autoComplete="off"
          spellCheck={false}
          autoFocus={autoFocus}
          placeholder={`Search ${UNIVERSE} stocks by symbol or name`}
          value={q}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={onKeyDown}
          onFocus={() => results && q.trim() && setOpen(true)}
          onBlur={() => setOpen(false)}
        />
        {size === "bar" && <kbd>{isMac ? "⌘K" : "Ctrl K"}</kbd>}
      </label>
      {showList && (
        <div className={s.pop}>
          {results.length > 0 && (
            <ul role="listbox" id={listId} aria-label="Matching stocks">
              {results.map((r, i) => (
                <li
                  key={r.isin}
                  id={optionId(i)}
                  role="option"
                  aria-selected={i === active}
                  className={s.opt}
                  onMouseDown={(e) => {
                    e.preventDefault();
                    go(r);
                  }}
                  onMouseEnter={() => setActive(i)}
                >
                  <b>{r.nse_symbol}</b> <span className={s.name}>{r.name}</span>{" "}
                  {r.sector && <span className={s.sector}>{r.sector}</span>}
                </li>
              ))}
            </ul>
          )}
          {results.length === 0 && (
            <div role="status" className={s.status}>
              {failed ? "Search is unavailable right now." : `No ${UNIVERSE} company matches “${q.trim()}”`}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
