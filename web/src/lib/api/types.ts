import type { components } from "./schema";

// The API serialises every field, defaults included, so a field the schema
// marks optional is always present (possibly null). Full<> says so, which
// keeps `?? []` noise out of every component.
type Full<T> = T extends (infer U)[]
  ? Full<U>[]
  : T extends object
    ? { [K in keyof T]-?: Full<T[K]> }
    : T;

type S = components["schemas"];

export type Instrument = Full<S["InstrumentOut"]>;
export type Signal = Full<S["SignalOut"]>;
export type SignalDetail = Full<S["SignalDetailOut"]>;
export type Explanation = Full<S["ExplanationOut"]>;
export type Family = Full<S["FamilyOut"]>;
export type Driver = Full<S["DriverOut"]>;
export type NewsMix = Full<S["NewsMixOut"]>;
export type RedFlag = Full<S["RedFlagOut"]>;
export type Financials = Full<S["FinancialsOut"]>;
export type Annual = Full<S["AnnualOut"]>;
export type Quarter = Full<S["QuarterOut"]>;
export type LineItem = Full<S["LineItemOut"]>;
export type PriceBar = Full<S["PriceBarOut"]>;
export type NewsItem = Full<S["NewsOut"]>;
export type ModelInfo = Full<S["ModelInfoOut"]>;
export type Health = Full<S["HealthOut"]>;
export type ImportResult = Full<S["ImportOut"]>;
export type Portfolio = Full<S["PortfolioOut"]>;
export type Holding = Full<S["HoldingOut"]>;
export type GroupShare = Full<S["GroupShareOut"]>;
