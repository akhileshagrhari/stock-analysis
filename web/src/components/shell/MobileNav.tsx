"use client";

import { useRef, useState, type ReactNode } from "react";
import { Brand } from "./Brand";
import { Icon } from "./icons";
import { SearchBox } from "./SearchBox";
import s from "./shell.module.css";

/** Phone top bar: menu drawer, brand, search toggle. Hidden above 720px. */
export function MobileNav({ drawer }: { drawer: ReactNode }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [searching, setSearching] = useState(false);

  return (
    <>
      <div className={s.mobileTop}>
        <button type="button" className={s.ic} aria-label="Open menu" onClick={() => dialog.current?.showModal()}>
          <Icon name="menu" />
        </button>
        <Brand />
        <button
          type="button"
          className={s.ic}
          aria-label={searching ? "Close search" : "Search"}
          aria-expanded={searching}
          onClick={() => setSearching((v) => !v)}
        >
          <Icon name="search" />
        </button>
      </div>
      {searching && (
        <div className={s.mobileSearch}>
          <SearchBox size="bar" autoFocus />
        </div>
      )}
      <dialog
        ref={dialog}
        className={s.drawer}
        aria-label="Menu"
        onClick={(e) => {
          // A link inside, or the backdrop, closes the drawer.
          const t = e.target as HTMLElement;
          if (t === dialog.current || t.closest("a")) dialog.current?.close();
        }}
      >
        <button type="button" className={s.drawerClose} aria-label="Close menu" onClick={() => dialog.current?.close()}>
          ✕
        </button>
        {drawer}
      </dialog>
    </>
  );
}
