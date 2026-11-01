import type { SVGProps } from 'react'

/** 状态页专用的小图标（自绘，不用 emoji）。 */
type P = SVGProps<SVGSVGElement> & { size?: number }

function base({ size = 20, ...rest }: P) {
  return {
    width: size, height: size, viewBox: '0 0 24 24',
    fill: 'none', stroke: 'currentColor', strokeWidth: 1.6,
    strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const,
    'aria-hidden': true, focusable: false, ...rest,
  }
}

export function IconHeart(p: P) {
  return (
    <svg {...base(p)}>
      <path d="M12 20s-7-4.4-7-9.2A4 4 0 0 1 12 7a4 4 0 0 1 7 3.8C19 15.6 12 20 12 20Z" />
    </svg>
  )
}

export function IconSparkle(p: P) {
  return (
    <svg {...base(p)}>
      <path d="M12 4l1.8 4.7L18.5 10.5 13.8 12.3 12 17l-1.8-4.7L5.5 10.5l4.7-1.8L12 4Z" />
    </svg>
  )
}

export function IconLeaf(p: P) {
  return (
    <svg {...base(p)}>
      <path d="M5 19c0-7 4.6-12 14-12 0 9-5 13-11 13-1.7 0-3-.5-3-1Z" />
      <path d="M5 19c2-4 5-6.5 9-8" opacity=".6" />
    </svg>
  )
}

export function IconMoon(p: P) {
  return (
    <svg {...base(p)}>
      <path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5Z" />
    </svg>
  )
}

export function IconBolt(p: P) {
  return (
    <svg {...base(p)}>
      <path d="M13 3 5.5 13.5H11L10 21l7.5-10.5H12L13 3Z" />
    </svg>
  )
}

export function IconAlert(p: P) {
  return (
    <svg {...base(p)}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.8v4.6M12 16.2v.1" />
    </svg>
  )
}
