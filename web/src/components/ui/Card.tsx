import type { ComponentProps } from "react";
import s from "./ui.module.css";

export function Card({ className, ...rest }: ComponentProps<"div">) {
  return <div className={className ? `${s.card} ${className}` : s.card} {...rest} />;
}
