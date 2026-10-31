import type { BubbleMessage } from '../types'

/**
 * 轻量气泡：只用于「操作反馈」和离线提示，不占聊天记录。
 * 同一时刻最多一条，避免堆叠成噪音。
 */
export function BubbleLayer({ bubbles }: { bubbles: BubbleMessage[] }) {
  if (bubbles.length === 0) return null
  return (
    <div className="bubble-layer" aria-live="polite" aria-atomic="false">
      {bubbles.map((bubble) => (
        <div key={bubble.id} className={'bubble bubble--' + bubble.tone}>
          {bubble.text}
        </div>
      ))}
    </div>
  )
}
