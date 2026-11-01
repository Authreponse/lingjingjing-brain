import { useEffect, useRef, useState } from 'react'

interface WhaleAvatarProps {
  size?: number
  /** 不在线时降低饱和度并停止眨眼。 */
  online?: boolean
  /** 静音时显示静音标记。 */
  muted?: boolean
  onClick?: () => void
  label?: string
}

/**
 * 圆形鲸鱼头像：呼吸 + 随机眨眼（纯 CSS 动画 + 一个极轻量的定时器）。
 * prefers-reduced-motion 下不启动眨眼定时器，CSS 也已把动画压到瞬时。
 */
export function WhaleAvatar({ size = 34, online = true, muted = false, onClick, label = '灵鲸鲸' }: WhaleAvatarProps) {
  const [blink, setBlink] = useState(false)
  const timer = useRef<number | undefined>(undefined)

  useEffect(() => {
    if (!online) return
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return
    let cancelled = false
    const schedule = () => {
      timer.current = window.setTimeout(() => {
        if (cancelled) return
        setBlink(true)
        window.setTimeout(() => { if (!cancelled) setBlink(false) }, 130)
        schedule()
      }, 2800 + Math.random() * 3600)
    }
    schedule()
    return () => {
      cancelled = true
      window.clearTimeout(timer.current)
    }
  }, [online])

  const shape = (
    <span
      className={
        'whale-avatar' +
        (online ? '' : ' is-offline') +
        (blink ? ' is-blinking' : '')
      }
      style={{ width: size, height: size }}
    >
      <svg viewBox="0 0 64 64" width={size} height={size} role="img" aria-label={label}>
        <defs>
          <linearGradient id="wa-body" x1="10" y1="12" x2="54" y2="52" gradientUnits="userSpaceOnUse">
            <stop stopColor="#8FE8FF" />
            <stop offset="1" stopColor="#5B7BFF" />
          </linearGradient>
        </defs>
        <g className="wa-breathe">
          {/* 尾巴 */}
          <path className="wa-tail" d="M16 34c-3-3.4-7.6-3.4-10.4.2 2.8 3.2 7.4 4.2 10.4 2.6" fill="url(#wa-body)" opacity=".72" />
          {/* 身体 */}
          <path
            d="M14 31.5C14 22 21.4 15 31.2 15c9.3 0 16.5 5.6 18 13.6 2.8-1.3 5.4-.4 6.9 1.7-2.1 2.5-4.9 3.3-7.5 2.6C47 40.8 40.2 46 31.4 46 21.4 46 14 40 14 31.5Z"
            fill="url(#wa-body)"
          />
          {/* 肚皮 */}
          <path d="M22 38c6 4 15 4.6 22 .6-2.4 4.6-7.6 7-13 6.4-4.4-.5-7.6-3-9-7Z" fill="#EAF7FF" opacity=".85" />
          {/* 眼睛（眨眼时压扁） */}
          <g className="wa-eyes" fill="#071018">
            <ellipse className="wa-eye" cx="33" cy="28.5" rx="2.2" ry="2.5" />
            <ellipse className="wa-eye" cx="41" cy="28.5" rx="2.2" ry="2.5" />
          </g>
          {/* 嘴 */}
          <path d="M35 34.5c1.2 1.3 3 1.3 4.2 0" stroke="#071018" strokeWidth="1.5" strokeLinecap="round" fill="none" opacity=".75" />
          {/* 腮红 */}
          <ellipse cx="29" cy="33" rx="2.6" ry="1.5" fill="#FF9FB2" opacity=".45" />
          <ellipse cx="45" cy="33" rx="2.4" ry="1.4" fill="#FF9FB2" opacity=".45" />
        </g>
      </svg>
      {muted && <span className="whale-avatar-mute" aria-hidden="true" />}
    </span>
  )

  if (!onClick) return shape
  return (
    <button
      type="button"
      className="whale-avatar-btn"
      onClick={onClick}
      aria-label={muted ? '恢复声音' : '静音'}
      title={muted ? '恢复声音' : '静音'}
      aria-pressed={muted}
    >
      {shape}
    </button>
  )
}
