import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { AppError, ChatRound as Round, ConnectionStatus } from '../types'
import { formatTime, roundSummary, shouldFollow } from '../lib/chat'
import { IconArrowDown, IconSend } from '../icons'

interface ChatPageProps {
  rounds: Round[]
  historyLoading: boolean
  historyError: AppError | null
  hasMore: boolean
  onLoadMore: () => void
  sending: boolean
  draft: string
  onDraft: (v: string) => void
  onSend: () => void
  connection: ConnectionStatus
  /** 本地追加的一条消息（刚发出、尚未进入历史）。 */
  liveRounds: Round[]
}

interface LayoutItem {
  round: Round
  /** 该轮 DOM 的 id，用于定位。 */
  key: string
}

function RoundCard({ round, isLive }: { round: Round; isLive: boolean }) {
  const time = formatTime(round.ts)
  return (
    <article
      className="round-card"
      id={'round-' + round.index}
      data-round={round.index}
      data-live={isLive ? '1' : undefined}
    >
      <div className="round-card-head">
        <span className="round-num">#{round.index + 1}</span>
        <time dateTime={round.ts}>{time}</time>
      </div>

      {round.user && (
        <div className="bubble bubble--user">{round.user.content}</div>
      )}
      {round.assistant && (
        <div className="bubble bubble--assistant">{round.assistant.content}</div>
      )}
      {round.user && !round.assistant && (
        <div className="bubble bubble--assistant bubble--pending">
          <span className="spinner" aria-hidden="true" />
          <span>灵鲸鲸正在想…</span>
        </div>
      )}
    </article>
  )
}

/**
 * 聊天页。
 *
 * 三个关键点（都是任务明确要求的）：
 *
 * 1. **历史消息不被吞**：滚动容器 .chat-scroll 是唯一滚动源，
 *    用 flex:1 + min-height:0 + overflow-y:auto。任何祖先都不能有固定高度把内容裁掉。
 *    内容按轮次渲染成独立的块，不需要虚拟列表或绝对定位。
 *
 * 2. **跟随与停止跟随**：用户贴近底部时自动跟随新消息；一旦上滚就停止跟随，
 *    并显示「回到底部」按钮。用 ref 记录是否跟随，避免闭包读到旧值。
 *
 * 3. **轮次定位条**：点击 → 平滑滚动到该轮；滚动 → 高亮当前轮。
 *    高亮通过 scroll 事件里按 DOM 位置判定，而不是依赖 IntersectionObserver，
 *    因为面板高度只有几百像素，Observer 的阈值在这种尺寸下不稳定。
 */
export function ChatPage({
  rounds: historyRounds,
  historyLoading,
  historyError,
  hasMore,
  onLoadMore,
  sending,
  draft,
  onDraft,
  onSend,
  connection,
  liveRounds,
}: ChatPageProps) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const followRef = useRef(true)
  const [showToBottom, setShowToBottom] = useState(false)
  const [activeRound, setActiveRound] = useState(0)

  // 历史轮次 + 本地未落库轮次拼接；本地轮次的 index 接在历史之后
  const items: LayoutItem[] = [
    ...historyRounds.map((r) => ({ round: r, key: r.id })),
    ...liveRounds.map((r, i) => ({
      round: { ...r, index: historyRounds.length + i },
      key: r.id,
    })),
  ]

  /** 记录用户位置：决定是否继续跟随，并更新「回到底部」按钮。 */
  const handleScroll = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    const near = shouldFollow(el.scrollTop, el.scrollHeight, el.clientHeight)
    followRef.current = near
    setShowToBottom(!near)

    // 当前轮：找到 top 最接近容器顶部的那个
    const cards = el.querySelectorAll<HTMLElement>('[data-round]')
    let current = 0
    let best = Number.POSITIVE_INFINITY
    cards.forEach((card) => {
      const delta = Math.abs(card.offsetTop - el.scrollTop - 8)
      if (delta < best) {
        best = delta
        current = Number(card.dataset.round ?? 0)
      }
    })
    setActiveRound(current)
  }, [])

  // 新内容到达：只有仍在跟随时才滚到底
  useLayoutEffect(() => {
    const el = scrollRef.current
    if (!el) return
    if (followRef.current) {
      el.scrollTop = el.scrollHeight
    }
  }, [items.length, sending])

  // 首次加载完成后滚到底部（历史是最新的在下面）
  const didInitialScroll = useRef(false)
  useEffect(() => {
    const el = scrollRef.current
    if (!el || didInitialScroll.current) return
    if (historyRounds.length === 0 && liveRounds.length === 0) return
    didInitialScroll.current = true
    requestAnimationFrame(() => {
      el.scrollTop = el.scrollHeight
      handleScroll()
    })
  }, [historyRounds.length, liveRounds.length, handleScroll])

  const scrollToBottom = () => {
    const el = scrollRef.current
    if (!el) return
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
    followRef.current = true
    setShowToBottom(false)
  }

  const jumpToRound = (index: number) => {
    const el = scrollRef.current
    if (!el) return
    const card = el.querySelector<HTMLElement>('[data-round="' + index + '"]')
    if (!card) return
    el.scrollTo({ top: Math.max(0, card.offsetTop - 8), behavior: 'smooth' })
    setActiveRound(index)
  }

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      onSend()
    }
  }

  const canSend = connection === 'online' && draft.trim().length > 0 && !sending

  return (
    <section className="page page--chat" aria-label="聊天">
      <div className="chat-layout">
        <div
          className="chat-scroll"
          ref={scrollRef}
          onScroll={handleScroll}
          role="log"
          aria-live="polite"
          aria-label="对话记录"
        >
          {historyLoading && items.length === 0 && (
            <div className="empty">
              <span className="spinner" aria-hidden="true" />
              <p>正在读取对话记录…</p>
            </div>
          )}

          {historyError && (
            <div className="alert alert--error" role="alert">
              <div>
                <strong>{historyError.message}</strong>
                <span>{historyError.hint}</span>
              </div>
            </div>
          )}

          {!historyLoading && !historyError && items.length === 0 && (
            <div className="empty">
              <strong>还没有聊过</strong>
              <p>在下面说点什么，第一条对话就从这里开始。</p>
            </div>
          )}

          {hasMore && items.length > 0 && (
            <button type="button" className="btn btn--ghost load-more" onClick={onLoadMore}>
              加载更早的记录
            </button>
          )}

          {items.map(({ round, key }) => {
            const live = liveRounds.some((r) => r.id === round.id)
            return (
              <div key={key} className="msg-enter">
                <RoundCard round={round} isLive={live} />
              </div>
            )
          })}
        </div>

        {/* 右侧轮次定位条 */}
        {items.length > 0 && (
          <nav className="rail-locate" aria-label="对话轮次定位">
            {items.map(({ round }) => (
              <div className="locate-item" key={'lb-' + round.id}>
                <span className="locate-index" aria-hidden="true">{round.index + 1}</span>
                <button
                  type="button"
                  className={'locate-bar' + (round.index === activeRound ? ' is-active' : '')}
                  onClick={() => jumpToRound(round.index)}
                  aria-label={'跳到第 ' + (round.index + 1) + ' 轮：' + roundSummary(round)}
                />
                <span className="locate-tip" role="tooltip">
                  <span className="locate-tip-num">#{round.index + 1}</span>
                  {roundSummary(round)}
                </span>
              </div>
            ))}
          </nav>
        )}
      </div>

      {showToBottom && (
        <button type="button" className="to-bottom" onClick={scrollToBottom}>
          <IconArrowDown size={15} />
          回到底部
        </button>
      )}

      <div className="composer">
        <div className="composer-box">
          <textarea
            ref={inputRef}
            value={draft}
            onChange={(e) => onDraft(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={
              connection === 'online' ? '和灵鲸鲸说点什么…' : '暂时连不上，先等等…'
            }
            aria-label="消息内容"
            rows={1}
            maxLength={4000}
            disabled={sending}
          />
          <button
            type="button"
            className="send-btn"
            onClick={onSend}
            disabled={!canSend}
            aria-label="发送"
            title="发送（Enter）"
          >
            {sending ? <span className="spinner" aria-hidden="true" /> : <IconSend size={18} />}
          </button>
        </div>
        <div className="composer-hint">
          <span>Enter 发送 · Shift + Enter 换行</span>
          <span>{draft.length}/4000</span>
        </div>
      </div>
    </section>
  )
}
