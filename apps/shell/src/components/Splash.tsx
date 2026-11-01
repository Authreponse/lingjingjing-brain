import { useEffect, useRef, useState, type CSSProperties } from 'react'
import { IconWhaleMark } from '../icons'

interface SplashProps {
  /** 初始化完成（例如首次健康检查返回）时可提前结束。 */
  ready: boolean
  onDone: () => void
  /** 最短展示时长，避免闪烁。 */
  minDuration?: number
}

/**
 * 启动画面：光波扩散 + 粒子向中心汇聚。
 *
 * 设计取舍：
 * - **不引入 canvas**：12 个粒子 + 3 圈光波用纯 CSS 就能做到 60fps，
 *   且不需要 rAF 循环、不占主线程，页面启动更快。
 * - **可跳过**：任意键 / 点击 / 关闭按钮立即结束，绝不拿动画拦路。
 * - **prefers-reduced-motion**：直接跳过动画（不播粒子与光波），
 *   只显示静态品牌与文字，并且缩短停留时间。
 */
export function Splash({ ready, onDone, minDuration = 1900 }: SplashProps) {
  const [leaving, setLeaving] = useState(false)
  const finished = useRef(false)
  const startedAt = useRef(Date.now())
  const reduced = typeof window !== 'undefined'
    && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

  const finish = () => {
    if (finished.current) return
    finished.current = true
    setLeaving(true)
    // 等淡出动画结束再卸载
    window.setTimeout(onDone, reduced ? 0 : 420)
  }

  // 到点自动结束
  useEffect(() => {
    const elapsed = Date.now() - startedAt.current
    const wait = Math.max(0, (reduced ? 450 : minDuration) - elapsed)
    const timer = window.setTimeout(finish, wait)
    return () => window.clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 后端就绪可以提前结束（但仍保证最短停留，避免一闪而过）
  useEffect(() => {
    if (!ready) return
    const elapsed = Date.now() - startedAt.current
    const wait = Math.max(0, (reduced ? 450 : 1100) - elapsed)
    const timer = window.setTimeout(finish, wait)
    return () => window.clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready])

  // 任意键跳过
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' || event.key === 'Enter' || event.key === ' ') finish()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const particles = Array.from({ length: 12 }, (_, i) => {
    const angle = (i / 12) * Math.PI * 2
    const dist = 96
    return {
      '--x': Math.cos(angle) * dist + 'px',
      '--y': Math.sin(angle) * dist + 'px',
      animationDelay: (i * 0.09).toFixed(2) + 's',
    } as CSSProperties
  })

  return (
    <div
      className={'splash' + (leaving ? ' is-out' : '')}
      role="dialog"
      aria-modal="true"
      aria-label="灵鲸鲸正在启动"
      onClick={finish}
    >
      {!reduced && (
        <>
          <span className="splash-wave" aria-hidden="true" />
          <span className="splash-wave" aria-hidden="true" />
          <span className="splash-wave" aria-hidden="true" />
        </>
      )}

      <div className="splash-core">
        {!reduced && (
          <div className="splash-particles" aria-hidden="true">
            {particles.map((style, i) => <i key={i} style={style} />)}
          </div>
        )}
        <IconWhaleMark size={76} />
      </div>

      <div style={{ position: 'absolute', top: '50%', left: 0, right: 0, marginTop: 46, textAlign: 'center' }}>
        <div className="splash-title">灵鲸鲸</div>
        <div className="splash-sub">正在唤醒</div>
      </div>

      {!reduced && <div className="splash-bar" aria-hidden="true"><i /></div>}

      <button
        type="button"
        className="splash-skip"
        onClick={(event) => {
          event.stopPropagation()
          finish()
        }}
      >
        跳过
      </button>
    </div>
  )
}
