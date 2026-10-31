import { useEffect, useRef, useState } from 'react'
import { API_BASE_URL, DEFAULT_TOKEN } from '../api'

interface SettingsDialogProps {
  token: string
  onSave: (token: string) => void
  onClose: () => void
}

/**
 * 设置弹层。键盘要求：
 * - Esc 关闭；
 * - 打开时焦点落在令牌输入框；
 * - Tab 焦点困在弹层内（简易焦点圈），关闭后焦点还给触发按钮。
 */
export function SettingsDialog({ token, onSave, onClose }: SettingsDialogProps) {
  const [draft, setDraft] = useState(token)
  const [show, setShow] = useState(false)
  const [hint, setHint] = useState('')
  const dialogRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    inputRef.current?.focus()
    inputRef.current?.select()
  }, [])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        onClose()
        return
      }
      if (event.key !== 'Tab') return
      const focusables = dialogRef.current?.querySelectorAll<HTMLElement>(
        'button, input, [href], select, textarea, [tabindex]:not([tabindex="-1"])',
      )
      if (!focusables || focusables.length === 0) return
      const first = focusables[0]
      const last = focusables[focusables.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  const save = () => {
    const next = draft.trim() || DEFAULT_TOKEN
    onSave(next)
  }

  return (
    <div
      className="scrim"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-title"
        ref={dialogRef}
      >
        <div className="dialog-head">
          <div>
            <h2 id="settings-title">连接设置</h2>
            <p>只保存在这台设备上</p>
          </div>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="关闭设置">×</button>
        </div>

        <div className="field">
          <label htmlFor="api-base">大脑服务地址</label>
          <input id="api-base" className="input" value={API_BASE_URL} readOnly tabIndex={-1} />
        </div>

        <div className="field">
          <label htmlFor="api-token">连接口令</label>
          <div style={{ display: 'flex', gap: 8 }}>
            <input
              id="api-token"
              ref={inputRef}
              className="input"
              type={show ? 'text' : 'password'}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') save()
              }}
              autoComplete="off"
              spellCheck={false}
            />
            <button type="button" className="btn" onClick={() => setShow((v) => !v)}>
              {show ? '隐藏' : '显示'}
            </button>
          </div>
          <p className="field-help">留空会恢复默认开发口令。改完点保存即可重连。</p>
        </div>

        {hint && <p className="field-help" style={{ color: 'var(--coral)' }}>{hint}</p>}

        <div className="dialog-actions">
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() => {
              setDraft(DEFAULT_TOKEN)
              setHint('已填回默认口令，记得点保存')
            }}
          >
            恢复默认
          </button>
          <button type="button" className="btn btn--primary" onClick={save}>保存并重连</button>
        </div>
      </div>
    </div>
  )
}
