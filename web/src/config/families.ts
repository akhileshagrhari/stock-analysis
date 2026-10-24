// What each factor family measures, copied from
// src/stockanalysis/serve/explain.py::FAMILY_MEANING (the API returns it per
// family on an explanation, but /v1/model doesn't carry it yet: R3).
export const FAMILY_MEANING: Record<string, string> = {
  value: "how cheap it is against its sector",
  quality: "balance-sheet strength and cash conversion",
  growth: "how fast revenue and profit are compounding",
  momentum: "price trend and relative strength",
  sentiment: "tone of the last 30 days of news",
};
