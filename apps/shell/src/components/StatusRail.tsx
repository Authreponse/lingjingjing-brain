import type { LingState } from '../types'
import { IconAlert } from '../icons'

interface StatusRailProps {
  state: LingState
  online: boolean
  muted: boolean
  onGoInteract: () => void
}

interface Badge {
  key: string
  label: string
  value: string
  tone: 'aqua' | 'mint' | 'amber' | 'rose' | 'muted'
}

const MOOD_SHORT: Record<string, string> = {
  开心: '开心', 平静: '平静', 好奇: '好奇', 困倦: '困', 难过: '难过',
  低落: '低落', 沮丧: '沮丧', 疲惫: '累', 饿坏了: '饿', 有点累: '累',
  happy: '开心', calm: '平静', curious: '好奇', sleepy: '困', sad: '难过',
}

/**
 * 左侧 40px 细栏：竖排小状态徽章，hover 出 tooltip，点击跳「互动」页。
 * 目的（线框要求）：不抢主区空间，但随时能看到能量的关键几项。
 */
export function StatusRail({ state, online, muted, onGoInteract }: StatusRailProps) {
  const badges: Badge[] = [];

  badges.push({
    key: 'online',
    label: '在线状态',
    value: online ? (muted ? '静音' : '在线') : '离线',
    tone: online ? (muted ? 'muted' : 'mint') : 'rose',
  })

  if (state.energy !== undefined) {
    badges.push({
      key: 'energy',
      label: '能量',
      value: Math.round(state.energy) + '%',
      tone: state.energy < 20 ? 'rose' : state.energy < 50 ? 'amber' : 'aqua',
    })
  }

  if (state.mood) {
    badges.push({ key: 'mood', label: '心情', value: MOOD_SHORT[state.mood] ?? state.mood, tone: 'aqua' })
  }

  if (state.hungry !== undefined || state.satiety !== undefined) {
    const hungry = state.hungry ?? (state.satiety !== undefined && state.satiety < 25)
    badges.push({
      key: 'hungry',
      label: '饥饿',
      value: hungry ? '饿' : '饱',
      tone: hungry ? 'amber' : 'muted',
    })
  }

  if (state.quiet) {
    badges.push({ key: 'quiet', label: '静默时段', value: '静默', tone: 'muted' })
  }

  return (
    <aside className="status-rail" aria-label="状态概览">
      {badges.map((b) => (
        <button
          key={b.key}
          type="button"
          className={'status-badge status-badge--' + b.tone}
          onClick={onGoInteract}
          aria-label={b.label + '：' + b.value + '，点击查看互动'}
        >
          <span className="status-badge-v">{b.value}</span>
          <span className="status-tip" role="tooltip">{b.label}：{b.value}</span>
        </button>
      ))}
      {badges.length <= 1 && (
        <span className="status-badge status-badge--muted" title="状态还没读到">
          <IconAlert size={13} />
          <span className="status-tip" role="tooltip">状态还没读到</span>
        </span>
      )}
    </aside>
  )
}
