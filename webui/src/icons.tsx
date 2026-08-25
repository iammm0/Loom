import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement>;

function Svg({ children, ...props }: IconProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      {children}
    </svg>
  );
}

export const Icons = {
  panel: (props: IconProps) => (
    <Svg {...props}>
      <rect x="3.5" y="4" width="17" height="16" rx="2.5" />
      <path d="M9.5 4v16" />
    </Svg>
  ),
  sparkle: (props: IconProps) => (
    <Svg {...props}>
      <path d="M12 3.5 13.6 9l5.4 1.6L13.6 12.2 12 17.5 10.4 12.2 5 10.6 10.4 9z" />
      <path d="M18.5 4.5v3M20 6h-3" />
    </Svg>
  ),
  list: (props: IconProps) => (
    <Svg {...props}>
      <path d="M9 7h11M9 12h11M9 17h11" />
      <circle cx="5" cy="7" r="1" fill="currentColor" stroke="none" />
      <circle cx="5" cy="12" r="1" fill="currentColor" stroke="none" />
      <circle cx="5" cy="17" r="1" fill="currentColor" stroke="none" />
    </Svg>
  ),
  folder: (props: IconProps) => (
    <Svg {...props}>
      <path d="M3.5 7.5A1.5 1.5 0 0 1 5 6h4.2l1.8 2H19a1.5 1.5 0 0 1 1.5 1.5v8A1.5 1.5 0 0 1 19 19H5A1.5 1.5 0 0 1 3.5 17.5z" />
    </Svg>
  ),
  tag: (props: IconProps) => (
    <Svg {...props}>
      <path d="M20.5 13.5 12.2 21.8a2 2 0 0 1-2.8 0L3.5 16a1.5 1.5 0 0 1-.4-1.1V8.5A1.5 1.5 0 0 1 4.6 7h6.4a1.5 1.5 0 0 1 1.1.4l8.4 8.4a1.5 1.5 0 0 1 0 2.1z" />
      <circle cx="8" cy="10" r="1" fill="currentColor" stroke="none" />
    </Svg>
  ),
  receipt: (props: IconProps) => (
    <Svg {...props}>
      <path d="M6.5 4.5h11v15l-2-1.2-2 1.2-1.5-1.2-1.5 1.2-2-1.2-2 1.2z" />
      <path d="M9 9h6M9 12.5h6" />
    </Svg>
  ),
  settings: (props: IconProps) => (
    <Svg {...props}>
      <circle cx="12" cy="12" r="3" />
      <path d="M12 3.5v2.2M12 18.3v2.2M4.8 6.5l1.6 1.6M17.6 15.9l1.6 1.6M3.5 12h2.2M18.3 12h2.2M4.8 17.5l1.6-1.6M17.6 8.1l1.6-1.6" />
    </Svg>
  ),
  publish: (props: IconProps) => (
    <Svg {...props}>
      <path d="M12 16.5V5.5" />
      <path d="M7.5 10 12 5.5 16.5 10" />
      <path d="M5 18.5h14" />
    </Svg>
  ),
  send: (props: IconProps) => (
    <Svg {...props} fill="currentColor" stroke="none">
      <path d="M12 4.5 19.5 16H4.5z" />
    </Svg>
  ),
  plus: (props: IconProps) => (
    <Svg {...props}>
      <path d="M12 6v12M6 12h12" />
    </Svg>
  ),
  sliders: (props: IconProps) => (
    <Svg {...props}>
      <path d="M4 7h16M4 17h16M8 4v6M16 14v6" />
    </Svg>
  ),
  check: (props: IconProps) => (
    <Svg {...props}>
      <path d="M5 12.5 9.5 17 19 7.5" />
    </Svg>
  ),
  x: (props: IconProps) => (
    <Svg {...props}>
      <path d="M6 6 18 18M18 6 6 18" />
    </Svg>
  ),
  chevron: (props: IconProps) => (
    <Svg {...props}>
      <path d="M9 6.5 15 12 9 17.5" />
    </Svg>
  ),
};
