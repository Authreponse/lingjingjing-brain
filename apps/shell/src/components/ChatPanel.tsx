import { useCallback, useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import type { ChatMessage } from '../types'

interface ChatPanelProps {
  messages: ChatMessage[]
  input: string
  onInputChange: (value: string) => void
  onSend: () => void
  sending: boolean
  disabled: boolean
  offline: boolean
}

/**
 * 打字机效果。
 *
 * 只在「最新一条助手消息」上打字，历史消息直接完整显示——否则每次滚动/重渲染
 * 都会把旧消息重新打一遍，很吵。用户一等不及可直接跳过（点击消息区域）。
 *
 * prefers-reduced-motion 下不打字，直接全量显示（这是无障碍要求，不是可选项）。
 */
function useTypewriter(text: string, enabled: boolean, speed = 18) {
  const [shown, setShown] = useState(enabled ? '' : text)
  const [done, setDone] = useState(!enabled)
  const [skipped, setSkipped] = useState(false)

  useEffect(() => {
    if (!enabled || skipped) {
      setShown(text)
      setDone(true)
      return
    }
    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    if (reduced) {
      setShown(text)
      setDone(true)
      return
    }
    setShown('')
    setDone(false)
    let i = 0
    const timer = window.setInterval(() => {
      // 每次多吐 1–2 个字符，读起来更自然
      i += 1 + (Math.random() < 0.35 ? 1 : 0)
      if (i >= text.length) {
        setShown(text)
        setDone(true)
        window.clearInterval(timer)
      } else {
        setShown(text.slice(0, i))
      }
    }, speed)
    return () => window.clearInterval(timer)
  }, [text, enabled, speed, skipped])

  const skip = () => {
    if (!done) {
      setSkipped(true)
      setShown(text)
      setDone(true)
    }
  }

  return { shown, done, skip }
}

function Bubble({
  message,
  typing,
  onTyped,
}: {
  message: ChatMessage
  typing: boolean
  onTyped: (id: string) => void
}) {
  const { shown, done, skip } = useTypewriter(message.content, typing)

  // 打完一遍后通知父级记住，之后不再重打（副作用放在 effect 里，避免渲染期副作用）。
  useEffect(() => {
    if (typing && done) onTyped(message.id)
  }, [typing, done, message.id, onTyped])

  return (
    <article className={'msg msg--' + message.role} onClick={skip}>
      <div className="msg-bubble">
        {typing ? (
          <>
            {shown}
            <span className="caret" aria-hidden="true" />
          </>
        ) : (
          message.content
        )}
        {message.imageUrl && (
          <img className="msg-image" src={message.imageUrl} alt={message.imageAlt ?? '灵鲸鲸画的图'} />
        )}
      </div>
      <div className="msg-meta">
        {message.role === 'user' ? '你' : message.role === 'assistant' ? '灵鲸鲸' : '提示'} · {message.time}
      </div>
    </article>
  )
}

/**
 * 聊天面板：气泡 + 输入框。
 * 键盘：Enter 发送、Shift+Enter 换行（输入法组词时不误发）。
 */
export function ChatPanel({ messages, input, onInputChange, onSend, sending, disabled, offline }: ChatPanelProps) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  // 最后一条助手消息才打字。用 id 记录已打完的，避免重渲染时重打。
  const lastAssistant = [...messages].reverse().find((m) => m.role === 'assistant')
  const [typedIds, setTypedIds] = useState<Set<string>>(() => new Set())
  const markTyped = useCallback((id: string) => {
    setTypedIds((current) => {
      if (current.has(id)) return current
      const next = new Set(current)
      next.add(id)
      return next
    })
  }, [])

  useEffect(() => {
    const el = scrollRef.current
    el?.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
  }, [messages, sending])

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      onSend()
    }
  }

  return (
    <section className="panel panel--chat" aria-label="聊天">
      <div className="chat-scroll" ref={scrollRef} role="log" aria-live="polite" aria-label="对话记录">
        {messages.map((message) => {
          const shouldType =
            message.id === lastAssistant?.id && !typedIds.has(message.id) && !sending
          return (
            <Bubble
              key={message.id}
              message={message}
              typing={shouldType}
              onTyped={markTyped}
            />
          )
        })}
        {sending && (
          <article className="msg msg--assistant">
            <div className="msg-bubble msg-bubble--thinking">
              <span className="dots" aria-label="灵鲸鲸正在回复"><i /><i /><i /></span>
            </div>
          </article>
        )}
      </div>

      <div className="composer">
        <div className="composer-box">
          <textarea
            ref={inputRef}
            value={input}
            onChange={(event) => onInputChange(event.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={offline ? '我暂时不在线，写好可以先等等…' : '和灵鲸鲸说点什么…'}
            aria-label="消息内容"
            rows={1}
            maxLength={4000}
            disabled={sending}
          />
          <button
            type="button"
            className="btn-send"
            onClick={onSend}
            disabled={disabled || sending || !input.trim()}
            aria-label="发送消息"
          >
            <span className="btn-send-icon" aria-hidden="true">↑</span>
          </button>
        </div>
        <div className="composer-hint">
          <span>Enter 发送 · Shift + Enter 换行</span>
          <span>{input.length}/4000</span>
        </div>
      </div>
    </section>
  )
}
