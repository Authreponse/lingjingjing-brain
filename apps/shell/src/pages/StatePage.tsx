import type { ReactElement } from 'react'
import type { AppError, ConnectionStatus, InteractionKind, LingState } from '../types'
import { IconAlert, IconHeart, IconLeaf, IconBolt, IconMoon, IconSparkle } from '../icons/quirk'

interface StatePageProps {
  state: LingState
  connection: ConnectionStatus
  error: AppError | null
  onRefresh: () => void
  refreshing: boolean
  interacting: InteractionKind | null
  onInteract: (kind: InteractionKind) => void
}

interface Metric { key: keyof LingState; label: string; color: string }
const METRICS: Metric[] = [
  { key: 'energy', label: '能量', color: '#6FE3FF' },
  { key: 'satiety', label: '饱食', color: '#FFC86B' },
  { key: 'affection', label: '亲近', color: '#63F5C4' },
  { key: 'tiredness', label: '疲惫', color: '#8B7BFF' },
]

const ACTIONS: { kind: InteractionKind; label: string; icon: (p: { size?: number }) => ReactElement }[] = [
  { kind: 'stroke', label: '摸摸', icon: IconHeart },
  { kind: 'poke', label: '戳一下', icon: IconSparkle },
  { kind: 'feed', label: '投喂', icon: IconLeaf },
  { kind: 'play', label: '一起玩', icon: IconBolt },
  { kind: 'wake', label: '叫醒', icon: IconMoon },
]

const MOOD_LABELS: Record<string, string> = {
  开心: '开心', 平静: '平静', 好奇: '好奇', 困倦: '困倦', 难过: '难过',
  低落: '低落', 沮丧: '沮丧', 疲惫: '疲惫', 有点累: '有点累', 饿坏了: '饿坏了',
  happy: '开心', calm: '平静', curious: '好奇', sleepy: '困倦', sad: '难过',
}

/**
 * 环形指标。
 *
 * 实现方式：**CSS conic-gradient**，而不是 SVG stroke-dasharray。
 * 原因：SVG 方案需要 rotate(-90deg) 才能从 12 点方向起画，而父级一旦出现
 * 任何 filter/backdrop-filter，旋转就会和合成层冲突，实测整段描边不渲染。
 * conic-gradient 没有这个问题，且过渡更平滑（--p 变量参与 transition）。
 */
function Ring({ value, color, label }: { value?: number; color: string; label: string }) {
  const known = value !== undefined
  const pct = known ? Math.min(100, Math.max(0, value)) : 0
  return (
    <div className="metric">
      <div
        className={'metric-ring' + (known ? '' : ' metric-ring--unknown')}
        style={{
          // --p 驱动 conic-gradient，配合 transition 实现弹性变化
          ['--p' as string]: pct + '%',
          ['--c' as string]: color,
        }}
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={known ? value : undefined}
        aria-valuetext={known ? value + '%' : '未知'}
      >
        <span className="metric-value">{known ? value : '—'}</span>
      </div>
      <span className="metric-cap">{label}{known ? '' : '（未知）'}</span>
    </div>
  )
}

export function StatePage({
  state, connection, error, onRefresh, refreshing, interacting, onInteract,
}: StatePageProps) {
  const offline = connection !== 'online'
  const mood = state.mood ? (MOOD_LABELS[state.mood] ?? state.mood) : undefined
  const quiet = state.quiet === true

  const headline = offline ? '暂时联系不上' : quiet ? '静音中' : (state.presence ?? '在呢')
  const detail = offline
    ? '连上以后会自动更新'
    : [mood, state.tired === true ? '想歇会儿' : undefined, state.hungry === true ? '有点饿' : undefined]
        .filter(Boolean).join(' · ') || '一切都好'

  return (
    <section className="page" aria-label="状态">
      <div className="page-head">
        <div>
          <h2 className="page-title">状态</h2>
          <p className="page-sub">她现在怎么样</p>
        </div>
        <div className="page-actions">
          <button type="button" className="btn btn--icon" onClick={onRefresh} disabled={refreshing} aria-label="刷新状态">
            <span className={refreshing ? 'spin' : undefined} style={{ display: 'inline-flex' }}>
              <IconSparkle size={16} />
            </span>
          </button>
        </div>
      </div>

      <div className="page-body">
        {error && (
          <div className="alert alert--error" role="alert" style={{ marginBottom: 'var(--s4)' }}>
            <IconAlert size={16} />
            <div><strong>{error.message}</strong><span>{error.hint}</span></div>
          </div>
        )}

        <div className="state-hero">
          <div className="state-copy">
            <h2>{headline}</h2>
            <p>{detail}</p>
          </div>
        </div>

        <div className="metrics">
          {METRICS.map((m) => (
            <Ring key={m.key} label={m.label} color={m.color} value={state[m.key] as number | undefined} />
          ))}
        </div>

        {!offline && (
          <div className="interactions" role="group" aria-label="互动">
            {ACTIONS.map((a) => {
              const Icon = a.icon
              return (
                <button
                  key={a.kind}
                  type="button"
                  className="interaction"
                  onClick={() => onInteract(a.kind)}
                  disabled={interacting !== null}
                >
                  <Icon size={20} />
                  <span>{interacting === a.kind ? '…' : a.label}</span>
                </button>
              )
            })}
          </div>
        )}
      </div>
    </section>
  )
}
