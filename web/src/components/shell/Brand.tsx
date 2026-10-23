import Link from "next/link";
import { BRAND } from "@/config/site";
import s from "./shell.module.css";

export function Brand() {
  return (
    <Link href="/" className={s.brand}>
      <span className={s.mark} aria-hidden="true">◆</span>
      {BRAND}
    </Link>
  );
}
