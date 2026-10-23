"use client";

import s from "./ui.module.css";

export type SegmentOption<T extends string> = { value: T; label: string };

/** A row of toggle buttons; aria-pressed marks the chosen one. */
export function Segmented<T extends string>({
  options,
  value,
  onChange,
  label,
  inline = false,
  className,
}: {
  options: SegmentOption<T>[];
  value: T;
  onChange: (v: T) => void;
  label: string;
  inline?: boolean;
  className?: string;
}) {
  const cls = [s.seg, inline ? s.segInline : "", className ?? ""].filter(Boolean).join(" ");
  return (
    <div role="group" aria-label={label} className={cls}>
      {options.map((o) => (
        <button key={o.value} type="button" aria-pressed={o.value === value} onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}
