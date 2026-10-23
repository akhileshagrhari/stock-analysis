import Link from "next/link";
import type { ReactNode } from "react";
import { BRAND, UNIVERSE } from "@/config/site";
import { todayIso } from "@/lib/format";
import { Brand } from "./Brand";
import { Disclaimer } from "./Disclaimer";
import { FreshnessCard } from "./FreshnessCard";
import { Icon } from "./icons";
import { MobileNav } from "./MobileNav";
import { NavList, type NavKey } from "./NavList";
import { SearchBox } from "./SearchBox";
import s from "./shell.module.css";

export type Crumb = { label: string; href?: string };
export type Freshness = { scores: string | null; prices?: string | null; filings?: string | null };

/** Sidebar, top bar, phone nav and the disclaimer around a page's content. */
export function AppShell({
  freshness,
  crumbs,
  active,
  today = todayIso(),
  children,
}: {
  freshness: Freshness;
  crumbs: Crumb[];
  active?: NavKey;
  today?: string;
  children: ReactNode;
}) {
  const fresh = <FreshnessCard today={today} {...freshness} />;
  return (
    <div className={s.viewport}>
      <div className={s.app}>
        <aside className={s.sidebar}>
          <Brand />
          <NavList active={active} />
          {fresh}
          <div className={s.sideFoot}>
            <span>{BRAND}</span>
            <span>{UNIVERSE}</span>
          </div>
        </aside>
        <main className={s.main}>
          <MobileNav
            drawer={
              <>
                <NavList active={active} />
                {fresh}
              </>
            }
          />
          <div className={s.topbar}>
            <nav className={s.crumbs} aria-label="Breadcrumb">
              <Link href="/" aria-label="Home">
                <Icon name="home" size={20} />
              </Link>
              {crumbs.map((c, i) => (
                <span key={i} style={{ display: "contents" }}>
                  <span aria-hidden="true">›</span>
                  {c.href ? <Link href={c.href}>{c.label}</Link> : <b aria-current="page">{c.label}</b>}
                </span>
              ))}
            </nav>
            <div className={s.topSearch}>
              <SearchBox size="bar" />
            </div>
          </div>
          {children}
          <Disclaimer />
        </main>
      </div>
    </div>
  );
}
