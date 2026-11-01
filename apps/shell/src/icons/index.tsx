import type { SVGProps } from 'react'

/**
 * 自绘 SVG 图标体系。
 *
 * 为什么不用 emoji：emoji 由系统字体渲染，各平台形状/颜色不一致，
 * 也无法继承 currentColor 做主题化，在暗色玻璃拟态里会显得突兀。
 *
 * 统一规范：24×24 视口、stroke 线宽 1.6、round 端点、fill: none，
 * 颜色一律用 currentColor，随上下文变色。
 */
type IconProps = SVGProps<SVGSVGElement> & { size?: number }

function base({ size = 20, ...props }: IconProps) {
  return {
    width: size,
    height: size,
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.6,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    'aria-hidden': true,
    focusable: false,
    ...props,
  }
}

export function IconChat(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M20 11.5a7.5 7.5 0 0 1-7.5 7.5H8l-4 3v-4.2A7.5 7.5 0 0 1 12.5 4 7.5 7.5 0 0 1 20 11.5Z" />
      <path d="M9 11.5h6M9 8.5h3" opacity=".55" />
    </svg>
  )
}

export function IconGallery(props: IconProps) {
  return (
    <svg {...base(props)}>
      <rect x="3" y="4.5" width="18" height="15" rx="3" />
      <path d="M3.5 16l4.2-4.2a2 2 0 0 1 2.8 0l3.3 3.3" />
      <path d="M13 13.2l2.1-2.1a2 2 0 0 1 2.8 0l2.6 2.6" />
      <circle cx="15.6" cy="8.6" r="1.5" />
    </svg>
  )
}

export function IconModels(props: IconProps) {
  return (
    <svg {...base(props)}>
      <rect x="3.5" y="3.5" width="7" height="7" rx="2" />
      <rect x="13.5" y="3.5" width="7" height="7" rx="2" />
      <rect x="3.5" y="13.5" width="7" height="7" rx="2" />
      <rect x="13.5" y="13.5" width="7" height="7" rx="2" />
      <circle cx="17" cy="17" r="1.6" fill="currentColor" stroke="none" />
    </svg>
  )
}

export function IconState(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M3 13c2.6 0 2.6-2.4 5.2-2.4S10.8 13 13.4 13s2.6-2.4 5.2-2.4S21 13 21 13" />
      <path d="M3 17.4c2.6 0 2.6-2.4 5.2-2.4s2.6 2.4 5.2 2.4 2.6-2.4 5.2-2.4S21 17.4 21 17.4" opacity=".5" />
      <circle cx="17.5" cy="6.5" r="2.2" />
    </svg>
  )
}

export function IconSettings(props: IconProps) {
  return (
    <svg {...base(props)}>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1.03 1.56V21a2 2 0 1 1-4 0v-.09A1.7 1.7 0 0 0 8.9 19.3a1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.7 1.7 0 0 0 4.7 15a1.7 1.7 0 0 0-1.56-1.03H3a2 2 0 1 1 0-4h.09A1.7 1.7 0 0 0 4.7 8.9a1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.7 1.7 0 0 0 9 4.7a1.7 1.7 0 0 0 1.03-1.56V3a2 2 0 1 1 4 0v.09A1.7 1.7 0 0 0 15 4.7a1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.7 1.7 0 0 0 19.3 9v.09a1.7 1.7 0 0 0 1.56 1.03H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.51 1.03Z" />
    </svg>
  )
}

export function IconSend(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M4.5 12h13" />
      <path d="M12.5 6.5 18 12l-5.5 5.5" />
    </svg>
  )
}

export function IconArrowDown(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M12 5v14M6.5 13.5 12 19l5.5-5.5" />
    </svg>
  )
}

export function IconClose(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M6 6l12 12M18 6 6 18" />
    </svg>
  )
}

export function IconCheck(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M4.5 12.5 9.5 17.5 19.5 6.5" />
    </svg>
  )
}

export function IconPlus(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M12 5v14M5 12h14" />
    </svg>
  )
}

export function IconEdit(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M4 20h4l10.5-10.5a2.1 2.1 0 0 0-3-3L5 17v3Z" />
      <path d="M13.5 6.5l3 3" />
    </svg>
  )
}

export function IconTrash(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M4 7h16M9.5 7V5.2A1.2 1.2 0 0 1 10.7 4h2.6a1.2 1.2 0 0 1 1.2 1.2V7" />
      <path d="M6.5 7l.8 11.3A2 2 0 0 0 9.3 20h5.4a2 2 0 0 0 2-1.7L17.5 7" />
      <path d="M10.5 11v5M13.5 11v5" opacity=".55" />
    </svg>
  )
}

export function IconBolt(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M13 3 5.5 13.5H11L10 21l7.5-10.5H12L13 3Z" />
    </svg>
  )
}

export function IconRefresh(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M20 12a8 8 0 1 1-2.6-5.9" />
      <path d="M20 4.5V10h-5.5" />
    </svg>
  )
}

export function IconAlert(props: IconProps) {
  return (
    <svg {...base(props)}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.8v4.6M12 16.2v.1" />
    </svg>
  )
}

export function IconWave(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M3 12.5c2.2 0 2.2-3 4.4-3s2.2 3 4.4 3 2.2-3 4.4-3 2.2 3 4.4 3" />
    </svg>
  )
}

export function IconChevronDown(props: IconProps) {
  return (
    <svg {...base(props)}>
      <path d="M6 9.5 12 15.5 18 9.5" />
    </svg>
  )
}

export function IconRefresh2(props: IconProps) {
  return <IconRefresh {...props} />
}

/** 工具箱（小鲸鱼的工具）。 */
export function IconToolbox(props: IconProps) {
  return (
    <svg {...base(props)}>
      <rect x="3" y="8.5" width="18" height="11" rx="2.5" />
      <path d="M8.5 8.5V6.8A2.3 2.3 0 0 1 10.8 4.5h2.4a2.3 2.3 0 0 1 2.3 2.3v1.7" />
      <path d="M3 13h18" opacity=".55" />
      <path d="M10.5 13v2h3v-2" opacity=".55" />
    </svg>
  )
}

/** 品牌标记：一只抽象的鲸鱼轮廓，用于 splash 与标题栏。 */
export function IconWhaleMark({ size = 48, ...props }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      fill="none"
      aria-hidden="true"
      focusable="false"
      {...props}
    >
      <defs>
        <linearGradient id="wmg" x1="8" y1="8" x2="56" y2="56" gradientUnits="userSpaceOnUse">
          <stop stopColor="#7BE0FF" />
          <stop offset="1" stopColor="#4B7BFF" />
        </linearGradient>
      </defs>
      <path
        d="M14 34c0-9 7.6-16 17.6-16 9.2 0 16.2 5.6 17.6 13.4 3-1.6 5.8-.6 7.4 1.6-2.2 2.6-5.2 3.4-8 2.6-1.4 8.6-8.6 14.4-17.6 14.4C21 50 14 43.6 14 34Z"
        fill="url(#wmg)"
        opacity=".92"
      />
      <path d="M14 34c-2.6-2.2-6.2-2.2-8.4.4 2.2 2.6 5.8 3.4 8.4 2.2" fill="url(#wmg)" opacity=".65" />
      <circle cx="38" cy="30" r="2.6" fill="#061018" />
      <path d="M30 40c2.4 2.4 6 2.4 8.4 0" stroke="#061018" strokeWidth="2" strokeLinecap="round" opacity=".7" />
      <path d="M33 17c0-3 2-5.5 5-6.5" stroke="#7BE0FF" strokeWidth="2" strokeLinecap="round" opacity=".6" />
    </svg>
  )
}
