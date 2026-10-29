// Line icons from the mock, 24x24, drawn with currentColor.
const paths = {
  home: <path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z" />,
  stocks: (<><circle cx="12" cy="12" r="9" /><path d="M8 14l3-3 2 2 3-4" /></>),
  signals: <path d="M4 19V9M10 19V5M16 19v-7M22 19H2" />,
  flag: <path d="M5 21V4M5 4h11l-2 4 2 4H5" />,
  sectors: (<><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /></>),
  screener: <path d="M3 5h18l-7 8v6l-4-2v-4z" />,
  learn: (<><circle cx="12" cy="12" r="9" /><path d="M12 8v5M12 16.5v.5" /></>),
  model: <path d="M4 4h16v16H4zM4 9h16M9 9v11" />,
  shield: <path d="M12 3 4 6v6c0 4.5 3.4 8.3 8 9 4.6-.7 8-4.5 8-9V6z" />,
  portfolio: <path d="M4 8h16v11a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1zM9 8V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v3M4 13h16" />,
  search: (<><circle cx="11" cy="11" r="7" /><path d="m20 20-4-4" /></>),
  menu: <path d="M4 7h16M4 12h16M4 17h16" />,
};

export type IconName = keyof typeof paths;

export function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
      {paths[name]}
    </svg>
  );
}
