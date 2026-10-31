import { useEffect, useRef, useState } from 'react'

interface WhaleProps {
  energy?: number
  satiety?: number
  tiredness?: number
  mood?: string
  presence?: string
  quiet?: boolean
  offline?: boolean
  size?: number
  /** 尺寸大时用「舞台」模式：加光晕和气泡，适合状态面板的主角。 */
  stage?: boolean
}

type Emotion = 'happy' | 'calm' | 'tired' | 'sad' | 'hungry' | 'sleep' | 'offline'

function classify(props: Omit<WhaleProps, 'size' | 'stage'>): Emotion {
  const { energy, satiety, tiredness, mood, presence, offline } = props
  const m = (mood ?? '').toLowerCase()
  const p = (presence ?? '').toLowerCase()

  if (offline || p === 'offline') return 'offline'
  if (
    p === 'asleep' || p === 'sleeping' || p === '睡了' ||
    /sleep|困|睡/.test(m) ||
    (tiredness !== undefined && tiredness > 85)
  ) return 'sleep'
  if (/sad|upset|frustrated|低落|难过|失落|沮丧/.test(m)) return 'sad'
  if (satiety !== undefined && satiety < 20) return 'hungry'
  if (/happy|excited|playful|开心|快乐|兴奋|顽皮|得意|抱抱/.test(m) || (energy !== undefined && energy > 75)) return 'happy'
  if (tiredness !== undefined && tiredness > 60) return 'tired'
  return 'calm'
}

/**
 * 原创 SVG 小蓝鲸，带「生命感」：
 * - 呼吸：身体整体轻微上下 + 极小的 y 轴缩放
 * - 游动：尾巴持续摆动；开心时摆幅更大、频率更快
 * - 眨眼：SVG 内部定时触发，随机间隔，眼睛用 scaleY 压扁表现
 * - 情绪反应：嘴角形态、腮红深浅、气泡/叹气/迷糊符号随状态切换
 *
 * 所有逻辑仍是**纯 CSS 动画**（无 3D 库）。prefers-reduced-motion 下
 * CSS 已把动画时长压到 0.01ms，同时这里也停止眨眼定时器，避免空转。
 */
export function Whale({
  energy,
  satiety,
  tiredness,
  mood,
  presence,
  quiet,
  offline,
  size = 96,
  stage = false,
}: WhaleProps) {
  const emotion = classify({ energy, satiety, tiredness, mood, presence, quiet, offline })
  const [blink, setBlink] = useState(false)
  const timer = useRef<number | undefined>(undefined)

  // 眨眼：随机 3–7 秒一次，睡着/离线时不眨。
  useEffect(() => {
    if (emotion === 'sleep' || emotion === 'offline') return
    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    if (reduced) return

    let cancelled = false
    const schedule = () => {
      const delay = 3000 + Math.random() * 4000
      timer.current = window.setTimeout(() => {
        if (cancelled) return
        setBlink(true)
        window.setTimeout(() => {
          if (!cancelled) setBlink(false)
        }, 140)
        schedule()
      }, delay)
    }
    schedule()
    return () => {
      cancelled = true
      window.clearTimeout(timer.current)
    }
  }, [emotion])

  const w = size
  const h = size * 0.8
  // 小于 72px 时（导航栏头像）：细节会糊成一团，改用简化渲染并把构图收进视野中心。
  const simplified = size < 72
  const label =
    emotion === 'offline' ? '灵鲸鲸，暂时联系不上'
      : emotion === 'sleep' ? '灵鲸鲸，正在休息'
        : '灵鲸鲸，心情' + (mood ?? '未知')

  // 情绪 → 嘴角路径
  const mouth =
    emotion === 'sleep' ? 'M144 104 Q150 100.5 156 104'
      : emotion === 'sad' ? 'M143 110 Q150 102 157 110'
        : emotion === 'hungry' ? 'M143 106 Q150 113 157 106'
          : emotion === 'happy' ? 'M140 101 Q150 115 160 101'
            : 'M143 104 Q150 110 158 104'

  return (
    <div
      className={
        'whale whale--' + emotion +
        (blink ? ' is-blinking' : '') +
        (quiet ? ' is-quiet' : '') +
        (stage ? ' whale--stage' : '') +
        (simplified ? ' whale--mini' : '')
      }
      style={{ width: w, height: h }}
    >
      {stage && <span className="whale-aura" aria-hidden="true" />}

      {/* mini 模式放大取景：裁掉四周空白，让鲸鱼占满头像位 */}
      <svg
        viewBox={simplified ? '24 30 180 120' : '0 0 220 176'}
        width={w}
        height={h}
        role="img"
        aria-label={label}
      >
        <defs>
          <linearGradient id="wb" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="#8FD4EA" />
            <stop offset="100%" stopColor="#4FA3C9" />
          </linearGradient>
          <linearGradient id="wbell" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#FFFFFF" />
            <stop offset="100%" stopColor="#D9F0F9" />
          </linearGradient>
          <radialGradient id="wblush" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="#FF9FB2" stopOpacity="0.55" />
            <stop offset="100%" stopColor="#FF9FB2" stopOpacity="0" />
          </radialGradient>
        </defs>

        {/* 呼吸 + 游动 的最外层 */}
        <g className="whale-body-wrap">
          {/* 尾巴：独立摆动，旋转轴在身体连接处 */}
          <g className="whale-tail">
            <path
              d="M57 99 C39 81 18 82 18 97 C18 111 36 116 52 111 C39 123 40 136 52 137 C66 137 72 122 67 109 Z"
              fill="#6FBEDD"
            />
          </g>

          <path
            d="M48 99 C48 61 80 40 122 43 C166 46 193 74 187 109 C181 140 144 149 102 140 C68 133 48 121 48 99 Z"
            fill="url(#wb)"
          />
          <path
            d="M66 113 C92 125 129 130 167 116 C157 140 126 145 99 138 C82 134 71 125 66 113 Z"
            fill="url(#wbell)"
          />

          {/* 腮红：随情绪变化浓度 */}
          <g className="whale-blush">
            <ellipse cx="126" cy="100" rx="13" ry="8" fill="url(#wblush)" />
            <ellipse cx="173" cy="100" rx="12" ry="7" fill="url(#wblush)" />
          </g>

          {/* 眼睛：眨眼时用 scaleY 压扁 */}
          <g className="whale-eyes">
            {emotion === 'sleep' || emotion === 'offline' ? (
              <g fill="none" stroke="#1E3440" strokeWidth="3.2" strokeLinecap="round">
                <path d="M130 88 Q136 93.5 142 88" />
                <path d="M157 88 Q163 93.5 169 88" />
              </g>
            ) : (
              <>
                <g className="whale-eye">
                  <circle cx="136" cy="87" r="5.2" fill="#1E3440" />
                  <circle cx="138" cy="84.6" r="1.9" fill="#fff" />
                </g>
                <g className="whale-eye">
                  <circle cx="163" cy="87" r="5.2" fill="#1E3440" />
                  <circle cx="165" cy="84.6" r="1.9" fill="#fff" />
                </g>
              </>
            )}
          </g>

          <path d={mouth} fill="none" stroke="#1E3440" strokeWidth="2.8" strokeLinecap="round" />

          {/* 呼吸的水柱：开心时更明显（mini 模式省略，太小看不清） */}
          {!simplified && (emotion === 'happy' || emotion === 'calm') && (
            <g className="whale-spout" fill="none" stroke="#9FDCEE" strokeWidth="4" strokeLinecap="round">
              <path d="M150 44 C148 32 152 24 158 18" />
            </g>
          )}
        </g>

        {/* 情绪符号（mini 模式下只在睡/离线时保留，其余省略） */}
        {!simplified && emotion === 'sleep' && (
          <g className="whale-zzz" aria-hidden="true" fill="#8A9BA6" fontFamily="system-ui" fontWeight="700">
            <text x="176" y="52" fontSize="13">z</text>
            <text x="189" y="38" fontSize="15">z</text>
          </g>
        )}
        {!simplified && emotion === 'happy' && (
          <g className="whale-sparkles" aria-hidden="true" fill="#F5C46B">
            <circle cx="196" cy="46" r="3" />
            <circle cx="205" cy="60" r="2" />
          </g>
        )}
        {!simplified && emotion === 'hungry' && (
          <g className="whale-hungry" aria-hidden="true">
            <circle cx="199" cy="66" r="10" fill="#FFF1D6" />
            <path d="M195 66 Q199 62 203 66" fill="none" stroke="#C9932F" strokeWidth="2" strokeLinecap="round" />
          </g>
        )}
        {emotion === 'offline' && (
          <g className="whale-offline-mark" aria-hidden="true">
            <circle cx="196" cy="50" r="11" fill="#D9725C" />
            <path d="M192 46 L200 54 M200 46 L192 54" stroke="#fff" strokeWidth="2.2" strokeLinecap="round" />
          </g>
        )}

        {/* 静音标记 */}
        {quiet && emotion !== 'offline' && (
          <g className="whale-mute" aria-hidden="true">
            <circle cx="197" cy="44" r="12" fill="#2E7FA8" />
            <path d="M191 40 L203 40 M191 48 L203 48" stroke="#fff" strokeWidth="2.2" strokeLinecap="round" />
          </g>
        )}
      </svg>
    </div>
  )
}
