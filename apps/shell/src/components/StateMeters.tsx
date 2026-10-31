import type { LingState } from '../types'

/**
 * 环形能量可视化。
 *
 * 设计取舍：
 * - 用 SVG stroke-dasharray 画环，避免 canvas 重绘开销；
 * - 数值变化时用 CSS transition 做「弹性」过渡（带轻微回弹的曲线），而不是 rAF 手写缓动；
 * - 未知值绝不画成 0：环保持极淡轨道色，中间显示「—」。
 */
function Ring({
  label,
  value,
  color,
  size = 92,
  stroke = 7,
  delay = 0,
}: {
  label: string
  value?: number
  color: string
  size?: number
  stroke?: number
  delay?: number
}) {
  const r = (size - stroke) / 2
  const c = 2 * Math.PI * r
  const known = value !== undefined
  const pct = known ? Math.min(100, Math.max(0, value)) : 0
  const offset = c - (pct / 100) * c

  return (
    <div className="ring" style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={'0 0 ' + size + ' ' + size} aria-hidden="true">
        <circle cx={size / 2} cy={size / 2} r={r} className="ring-track" strokeWidth={stroke} fill="none" />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          className="ring-value"
          strokeWidth={stroke}
          fill="none"
          stroke={color}
          strokeLinecap="round"
          strokeDasharray={c}
          strokeDashoffset={known ? offset : c}
          style={{ transitionDelay: delay + 'ms' }}
        />
      </svg>
      <div className="ring-center">
        <strong>{known ? value : '—'}</strong>
        {known && <span>%</span>}
      </div>
      <span className="sr-only">{label}{known ? ' ' + value + '%' : '未知'}</span>
    </div>
  )
}

/**
 * 波浪图：能量越高水位越高。两层正弦波叠加并水平平移形成流动感。
 * 只用 transform 动画，GPU 友好。
 */
function Wave({ value }: { value?: number }) {
  const known = value !== undefined
  const pct = known ? Math.min(100, Math.max(0, value)) : 0
  const top = 100 - pct

  return (
    <div className="wave" role="img" aria-label={known ? '能量水位 ' + value + '%' : '能量水位未知'}>
      <svg viewBox="0 0 200 64" preserveAspectRatio="none" aria-hidden="true">
        <defs>
          <linearGradient id="waveFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#7FCBE6" stopOpacity="0.55" />
            <stop offset="100%" stopColor="#2E7FA8" stopOpacity="0.8" />
          </linearGradient>
        </defs>
        <rect x="0" y="0" width="200" height="64" className="wave-track" />
        {known && (
          <g className="wave-crest" style={{ transform: 'translateY(' + top + '%)' }}>
            <g className="wave-move">
              <path d="M0 10 Q25 0 50 10 T100 10 T150 10 T200 10 V64 H0 Z" fill="url(#waveFill)" />
            </g>
          </g>
        )}
      </svg>
    </div>
  )
}

const METRICS = [
  { key: 'energy', label: '能量', color: '#2E7FA8' },
  { key: 'satiety', label: '饱食', color: '#C9932F' },
  { key: 'affection', label: '亲近', color: '#4E9C7F' },
  { key: 'tiredness', label: '疲惫', color: '#8F96C9' },
] as const

/**
 * 状态可视化：四个环形 + 能量波浪。任一字段缺失不影响其它三项。
 */
export function StateMeters({ state, compact = false }: { state: LingState; compact?: boolean }) {
  return (
    <div className={compact ? 'viz viz--compact' : 'viz'}>
      <div className="viz-rings">
        {METRICS.map((m, i) => (
          <div className="viz-item" key={m.key}>
            <Ring label={m.label} value={state[m.key]} color={m.color} size={compact ? 72 : 90} delay={i * 70} />
            <span className="viz-caption">{m.label}</span>
          </div>
        ))}
      </div>
      <div className="viz-wave-wrap">
        <div className="viz-wave-head">
          <span>能量水位</span>
          <strong>{state.energy !== undefined ? state.energy + '%' : '未知'}</strong>
        </div>
        <Wave value={state.energy} />
      </div>
    </div>
  )
}
