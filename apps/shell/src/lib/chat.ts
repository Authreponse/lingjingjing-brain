import type { ChatRound, HistoryMessage } from '../types'

/**
 * 把历史消息切成「对话轮次」。
 *
 * 契约 §1 明确规定：后端不预分组，前端负责把「一条 user + 紧随其后的 assistant」分成一组。
 *
 * 边界情况（都会遇到，必须处理）：
 * - 两条 user 连在一起（上一轮没得到回复）→ 各开一轮，不合并；
 * - assistant 出现在没有任何 user 之前（例如开场白）→ 单独成轮，user 为空；
 * - 最后一条 user 还没有 assistant（正在等回复）→ 成轮，assistant 为空。
 *
 * 这是一个纯函数，不依赖 DOM，便于单测。
 */
export function groupRounds(items: HistoryMessage[]): ChatRound[] {
  const rounds: ChatRound[] = []
  let pendingUser: HistoryMessage | undefined

  const push = (user?: HistoryMessage, assistant?: HistoryMessage) => {
    const ts = user?.ts ?? assistant?.ts ?? new Date(0).toISOString()
    rounds.push({
      id: user?.id ?? assistant?.id ?? 'round-' + rounds.length,
      index: rounds.length,
      user,
      assistant,
      ts,
    })
  }

  for (const item of items) {
    if (item.role === 'user') {
      // 上一轮 user 没有回复：先把它单独收尾，再开新的一轮
      if (pendingUser) push(pendingUser, undefined)
      pendingUser = item
    } else {
      if (pendingUser) {
        push(pendingUser, item)
        pendingUser = undefined
      } else {
        push(undefined, item)
      }
    }
  }
  if (pendingUser) push(pendingUser, undefined)

  return rounds
}

/** 轮次定位条上 hover 显示的摘要：截断到一行。 */
export function roundSummary(round: ChatRound): string {
  const text = round.user?.content ?? round.assistant?.content ?? ''
  const clean = text.replace(/\s+/g, ' ').trim()
  if (!clean) return '（空消息）'
  return clean.length > 42 ? clean.slice(0, 42) + '…' : clean
}

/** 轮次的时间标签（本地 HH:MM）。ts 解析失败时返回空串，不抛错。 */
export function formatTime(ts: string): string {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
}

/**
 * 是否应「跟随到底部」。
 * 只有当用户已经贴近底部时才自动跟随；用户主动上滚后停止跟随。
 * threshold 给 80px：轻微滚动（例如读刚才那条）不应立刻打断跟随。
 */
export function shouldFollow(scrollTop: number, scrollHeight: number, clientHeight: number, threshold = 80): boolean {
  const distance = scrollHeight - scrollTop - clientHeight
  return distance <= threshold
}
