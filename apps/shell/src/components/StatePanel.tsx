import { describeState, presenceLabel } from '../status'
import type { ConnectionStatus, InteractionKind, LingState } from '../types'
import { StateMeters } from './StateMeters'
import { Whale } from './Whale'

interface StatePanelProps {
  state: LingState
  connection: ConnectionStatus
  hasState: boolean
  interacting: InteractionKind | null
  onInteract: (kind: InteractionKind) => void
  onRefresh: () => void
  refreshing: boolean
}

const ACTIONS: { kind: InteractionKind; label: string; icon: string }[] = [
  { kind: 'stroke', label: '摸摸', icon: '✋' },
  { kind: 'poke', label: '戳一下', icon: '👆' },
  { kind: 'feed', label: '投喂', icon: '🍚' },
  { kind: 'play', label: '一起玩', icon: '🎈' },
  { kind: 'wake', label: '叫醒', icon: '☀' },
]

/**
 * 状态面板。
 * 指标用环形 + 波浪而不是横条：环形在 400px 窄窗里能一行放四个，横向条形会挤成一列。
 */
export function StatePanel({
  state,
  connection,
  hasState,
  interacting,
  onInteract,
  onRefresh,
  refreshing,
}: StatePanelProps) {
  const display = describeState({ state, connection, hasState })
  const presence = presenceLabel(state.presence)
  const quiet = state.quiet === true
  const offline = connection === 'offline'

  return (
    <section className="panel" aria-label="状态">
      <div className="panel-head">
        <div>
          <h2 className="panel-title">她现在怎么样</h2>
          <p className="panel-sub">点一点就能互动</p>
        </div>
        <button type="button" className="btn btn--icon" onClick={onRefresh} disabled={refreshing} aria-label="刷新状态">
          <span className={refreshing ? 'spin' : ''} aria-hidden="true">↻</span>
        </button>
      </div>

      <div className="panel-body state-body">
        <div className="state-hero">
          <Whale
            energy={state.energy}
            satiety={state.satiety}
            tiredness={state.tiredness}
            mood={state.mood}
            presence={state.presence}
            quiet={quiet}
            offline={offline}
            size={132}
            stage
          />
          <div className="state-hero-copy">
            <h2>{display.headline}</h2>
            <p>{display.detail}</p>
            {presence && !quiet && <span className="chip">{presence}</span>}
          </div>
        </div>

        {display.fallback && (
          <div className="alert alert--warn" role="status">
            <div>
              <strong>{offline ? '这是上次记住的样子' : '状态不完整'}</strong>
              <span>能显示的都显示了，缺的部分先空着，不猜。</span>
            </div>
          </div>
        )}

        <StateMeters state={state} />

        <div className="interactions" role="group" aria-label="互动">
          {ACTIONS.map((action) => (
            <button
              key={action.kind}
              type="button"
              className="interaction-btn"
              onClick={() => onInteract(action.kind)}
              disabled={interacting !== null || offline}
            >
              <span className="interaction-icon" aria-hidden="true">{action.icon}</span>
              <span>{interacting === action.kind ? '…' : action.label}</span>
            </button>
          ))}
        </div>
        {offline && <p className="field-help">她现在不在线，互动等连上以后再来。</p>}
      </div>
    </section>
  )
}
