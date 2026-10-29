import Link from "next/link";
import { Icon, type IconName } from "./icons";
import s from "./shell.module.css";

export type NavKey = "home" | "stocks" | "signals" | "red-flags" | "learn" | "disclaimer" | "portfolio";

type Item = { key?: NavKey; label: string; href?: string; icon: IconName; soon?: boolean };

const GROUPS: { heading?: string; items: Item[] }[] = [
  {
    items: [
      { key: "home", label: "Home", href: "/", icon: "home" },
      { key: "stocks", label: "Stocks", href: "/#search", icon: "stocks" },
    ],
  },
  {
    heading: "Discover",
    items: [
      { key: "signals", label: "Signals", href: "/signals", icon: "signals" },
      { key: "red-flags", label: "Red flags", href: "/red-flags", icon: "flag" },
      { label: "Sectors", href: "/signals?focus=sector", icon: "sectors" },
      { label: "Screener", icon: "screener", soon: true },
    ],
  },
  {
    heading: "Learn",
    items: [
      { key: "learn", label: "How scores work", href: "/learn", icon: "learn" },
      { label: "Model & weights", href: "/learn#model", icon: "model" },
      { key: "disclaimer", label: "Disclaimer", href: "/disclaimer", icon: "shield" },
    ],
  },
];

const PORTFOLIO: Item = { key: "portfolio", label: "Portfolio", href: "/portfolio", icon: "portfolio" };

export function NavList({ active, accounts }: { active?: NavKey; accounts: boolean }) {
  // Portfolio sits in the first group, after Stocks, only when accounts exist.
  const groups = accounts ? [{ ...GROUPS[0], items: [...GROUPS[0].items, PORTFOLIO] }, ...GROUPS.slice(1)] : GROUPS;
  return (
    <>
      {groups.map((g, i) => (
        <div key={i}>
          {g.heading && <div className={s.navH}>{g.heading}</div>}
          <nav className={s.nav} aria-label={g.heading ?? "Main"}>
            {g.items.map((it) =>
              it.href ? (
                <Link key={it.label} href={it.href} aria-current={it.key && it.key === active ? "page" : undefined}>
                  <Icon name={it.icon} />
                  {it.label}
                </Link>
              ) : (
                <span key={it.label} className={s.disabled} aria-disabled="true">
                  <Icon name={it.icon} />
                  {it.label}
                  {it.soon && <span className={s.soon}>soon</span>}
                </span>
              ),
            )}
          </nav>
        </div>
      ))}
    </>
  );
}
