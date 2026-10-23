// Pull real API responses into tests/fixtures/, so tests run against the
// shapes and awkward cases the live data actually has. Needs the API running.
// A 404 is written as the JSON literal `null`, as the client returns it.
import { mkdir, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const API = `${process.env.SA_API_URL ?? "http://localhost:8000"}/v1`;
const OUT = join(dirname(fileURLToPath(import.meta.url)), "..", "tests", "fixtures");
const SYMBOLS = ["RELIANCE", "ABB", "HDFCBANK", "CHOLAFIN", "ADANIENSOL"];
const SIGNAL_DATES = ["2026-08-14", "2026-08-15", "2026-08-16"];

async function get(path) {
  const res = await fetch(API + path);
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`${res.status} for ${path}`);
  return res.json();
}

async function save(file, data) {
  const path = join(OUT, file);
  await mkdir(dirname(path), { recursive: true });
  await writeFile(path, JSON.stringify(data, null, 2) + "\n");
  console.log("wrote", file);
}

const model = await get("/model");
await save("model.json", model);
await save("health.json", await get("/health"));
await save("sectors.json", await get("/sectors"));
await save("red-flags.json", await get("/signals/red-flags?limit=1000"));
for (const date of SIGNAL_DATES) {
  await save(`signals-${date}.json`, await get(`/signals?as_of=${date}&limit=1000`));
}

for (const symbol of SYMBOLS) {
  const inst = await get(`/instruments/by-symbol/${encodeURIComponent(symbol)}`);
  const base = `/instruments/${inst.isin}`;
  const explanation = await get(`${base}/explanation`);
  await save(`${symbol}/instrument.json`, inst);
  await save(`${symbol}/explanation.json`, explanation);
  await save(`${symbol}/history.json`, await get(`${base}/history?limit=500`));
  await save(`${symbol}/prices.json`, await get(`${base}/prices?from=2000-01-01`));
  await save(`${symbol}/financials.json`, await get(`${base}/financials?quarters=12`));
  await save(`${symbol}/news.json`, await get(`${base}/news?limit=20`));
  const peersAsOf = explanation?.as_of ?? model.scored_dates[0];
  const sector = encodeURIComponent(inst.sector ?? "");
  await save(`${symbol}/peers.json`, await get(`/signals?sector=${sector}&as_of=${peersAsOf}&limit=1000`));
}
