import s from "./ui.module.css";

/** An ⓘ that explains a figure on hover or keyboard focus. */
export function InfoTip({ text }: { text: string }) {
  return (
    <i className={s.info} tabIndex={0} role="img" aria-label={text} data-tip={text}>
      i
    </i>
  );
}
