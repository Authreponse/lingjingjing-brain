import type { LingState } from './types'

/**
 * 纯展示逻辑：把后端状态翻译成人话。不依赖 React / DOM / Tauri，便于单测与评审。
 * 原则：字段缺席就降级，绝不编造数值；离线时明确告诉用户，而不是假装一切正常。
 */

const MOOD_LABELS: Record<string, string> = {
  happy: '开心',
  calm: '平静',
  curious: '好奇',
  sleepy: '困倦',
  sad: '难过',
  upset: '低落',
  frustrated: '沮丧',
  angry: '生气',
  excited: '兴奋',
  focused: '专注',
  thinking: '在想事情',
  neutral: '平静',
  playful: '顽皮',
  tired: '疲惫',
  hungry: '饿',
  bored: '无聊',
  lonely: '孤单',
  proud: '自豪',
  affectionate: '亲近',
}

const PRESENCE_LABELS: Record<string, string> = {
  online: '在呢',
  present: '在呢',
  active: '在呢',
  offline: '离线',
  asleep: '睡着了',
  sleeping: '睡着了',
  away: '走开了',
  busy: '忙着',
  idle: '发呆',
  hidden: '躲起来',
  在: '在呢',
  睡了: '睡着了',
  静默: '静音中',
  打盹: '打盹',
  离开: '走开了',
}

export function moodLabel(mood?: string): string | undefined {
  if (!mood) return undefined
  return MOOD_LABELS[mood.toLowerCase()] ?? mood
}

export function presenceLabel(presence?: string): string | undefined {
  if (!presence) return undefined
  return PRESENCE_LABELS[presence.toLowerCase()] ?? presence
}

export interface DisplayState {
  headline: string
  detail: string
  /** 离线兜底时为 true：界面要明说这是旧数据。 */
  fallback: boolean
}

export interface DescribeInput {
  state: LingState
  connection: 'connecting' | 'online' | 'offline' | 'stale'
  hasState: boolean
}

/** 状态 → 一句标题 + 一句描述。任何字段缺席都安全降级。 */
export function describeState({ state, connection, hasState }: DescribeInput): DisplayState {
  if (connection === 'offline') {
    return {
      headline: '暂时联系不上',
      detail: hasState ? '下面是上次记住的样子，我回来就更新' : '我会先在本地陪着你',
      fallback: true,
    }
  }
  if (connection === 'connecting') {
    return { headline: '正在找我…', detail: '马上就好', fallback: false }
  }
  if (!hasState) {
    return { headline: '状态未知', detail: '这会儿还没对上，稍后再看看', fallback: true }
  }

  const presence = presenceLabel(state.presence)
  const quiet = state.quiet === true
  const headline = quiet ? '静音中' : presence ?? '在呢'

  const parts: string[] = []
  const mood = moodLabel(state.mood)
  if (mood) parts.push(mood)

  const hungry = state.hungry ?? (state.satiety !== undefined ? state.satiety < 25 : undefined)
  const tired = state.tired ?? (state.tiredness !== undefined ? state.tiredness > 70 : undefined)
  if (tired === true) parts.push('想歇会儿')
  if (hungry === true) parts.push('有点饿')
  if (state.energy !== undefined && state.energy < 20) parts.push('力气不多')

  return {
    headline,
    detail: parts.length > 0 ? parts.join(' · ') : '一切都好',
    fallback: false,
  }
}

/** 电量条语义：低电量要换颜色，而不只是条短了。 */
export function energyTone(energy?: number): 'low' | 'mid' | 'good' | 'unknown' {
  if (energy === undefined) return 'unknown'
  if (energy < 20) return 'low'
  if (energy < 50) return 'mid'
  return 'good'
}

/** 把 0–100 转成给读屏用的自然语言，避免只念数字。 */
export function meterText(label: string, value?: number): string {
  return value === undefined ? label + '未知' : label + ' ' + value + '%'
}

/**
 * 顶部状态点的语气：
 * - online 且非静音 → 正常
 * - 静音 → 安静
 * - 离线 → 断开
 */
export function statusTone(
  connection: 'connecting' | 'online' | 'offline' | 'stale',
  quiet: boolean,
): 'ok' | 'quiet' | 'busy' | 'down' {
  if (connection === 'offline') return 'down'
  if (connection === 'connecting') return 'busy'
  if (quiet) return 'quiet'
  return 'ok'
}
