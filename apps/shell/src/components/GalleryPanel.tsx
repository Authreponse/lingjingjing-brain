import { useEffect, useRef, useState } from 'react'
import type { ArtworkItem } from '../types'

interface GalleryPanelProps {
  artworks: ArtworkItem[]
  urls: Record<string, string>
  loading: boolean
  generating: boolean
  error: { message: string; hint?: string } | null
  onGenerate: (prompt: string, reason: string) => void
  onRefresh: () => void
}

/**
 * 瀑布流。
 *
 * 为什么不用 CSS columns：columns 会打乱 DOM 阅读顺序（读屏和 Tab 顺序会跳）。
 * 这里用「按列分配」把每一列包成一个 div，视觉上瀑布流、语义上仍是顺序列表。
 * 列数随容器宽度切换（1/2/3/4），用 CSS 变量控制，渲染时按估算高度贪心分配。
 */
const COLUMN_BREAKPOINTS = [
  { minWidth: 1100, cols: 4 },
  { minWidth: 780, cols: 3 },
  { minWidth: 520, cols: 2 },
  { minWidth: 0, cols: 1 },
]

function estimateHeight(item: ArtworkItem): number {
  // 图片是 1:1，caption 两行约 30px；prompt 越长 caption 越可能占满两行。
  return 1 + Math.min(2, Math.ceil((item.reason || item.prompt).length / 18)) * 0.18
}

/** 按容器宽度决定列数，实现 400–1400px 的响应式瀑布流。 */
function useColumns(ref: React.RefObject<HTMLElement | null>): number {
  const [cols, setCols] = useState(2)

  useEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const compute = (width: number) => {
      const next = COLUMN_BREAKPOINTS.find((bp) => width >= bp.minWidth)?.cols ?? 1
      setCols(next)
    }
    compute(el.clientWidth)
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) compute(entry.contentRect.width)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [ref])

  return cols
}

/**
 * 画廊 + 生图。
 * 悬停预览：图片轻微放大 + 遮罩浮出完整 reason，不打开弹窗（避免打断）。
 * 点击图片打开大图查看，Esc 关闭。
 */
export function GalleryPanel({
  artworks,
  urls,
  loading,
  generating,
  error,
  onGenerate,
  onRefresh,
}: GalleryPanelProps) {
  const [formOpen, setFormOpen] = useState(false)
  const [prompt, setPrompt] = useState('')
  const [reason, setReason] = useState('')
  const [preview, setPreview] = useState<ArtworkItem | null>(null)
  const bodyRef = useRef<HTMLDivElement>(null)
  const columns = useColumns(bodyRef)

  const submit = () => {
    const p = prompt.trim()
    const r = reason.trim()
    if (!p || !r) return
    onGenerate(p, r)
    setPrompt('')
    setReason('')
  }

  // 贪心分配列：每次都放进当前最矮的一列
  const buckets: ArtworkItem[][] = Array.from({ length: columns }, () => [])
  const heights = new Array(columns).fill(0)
  for (const item of artworks) {
    const target = heights.indexOf(Math.min(...heights))
    buckets[target].push(item)
    heights[target] += estimateHeight(item)
  }

  return (
    <section className="panel" aria-label="画廊">
      <div className="panel-head">
        <div>
          <h2 className="panel-title">画廊</h2>
          <p className="panel-sub">我画过的东西都在这儿</p>
        </div>
        <div className="panel-head-actions">
          <button type="button" className="btn btn--icon" onClick={onRefresh} disabled={loading} aria-label="刷新画廊">
            <span className={loading ? 'spin' : ''} aria-hidden="true">↻</span>
          </button>
          <button
            type="button"
            className={formOpen ? 'btn' : 'btn btn--primary'}
            onClick={() => setFormOpen((v) => !v)}
            aria-expanded={formOpen}
          >
            {formOpen ? '收起' : '画一张'}
          </button>
        </div>
      </div>

      <div className="panel-body" ref={bodyRef}>
        {error && (
          <div className="alert alert--error" role="alert" style={{ marginBottom: 16 }}>
            <div>
              <strong>{error.message}</strong>
              {error.hint && <span>{error.hint}</span>}
            </div>
          </div>
        )}

        {formOpen && (
          <div className="gen-form">
            <div className="field">
              <label htmlFor="art-prompt">画什么</label>
              <textarea
                id="art-prompt"
                className="textarea"
                value={prompt}
                onChange={(event) => setPrompt(event.target.value)}
                placeholder="比如：在月光下吹泡泡的小蓝鲸"
                maxLength={1200}
              />
            </div>
            <div className="field">
              <label htmlFor="art-reason">为什么想画</label>
              <input
                id="art-reason"
                className="input"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') submit()
                }}
                placeholder="比如：今天很开心，想留个纪念"
                maxLength={400}
              />
              <p className="field-help">这个会记进作品记录里，以后回看会更有意思。</p>
            </div>
            <button
              type="button"
              className="btn btn--primary"
              onClick={submit}
              disabled={generating || !prompt.trim() || !reason.trim()}
            >
              {generating ? '正在画…' : '开始画'}
            </button>
          </div>
        )}

        {loading && artworks.length === 0 ? (
          <div className="skeleton-grid" aria-label="正在加载">
            {Array.from({ length: 6 }).map((_, i) => <div key={i} className="skeleton" style={{ height: 140 + (i % 3) * 40 }} />)}
          </div>
        ) : artworks.length === 0 ? (
          <div className="empty">
            <strong>这里还空着</strong>
            <p>点右上角「画一张」，我画好了会放进来</p>
          </div>
        ) : (
          <div className="masonry">
            {buckets.map((bucket, ci) => (
              <div className="masonry-col" key={ci}>
                {bucket.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    className="art-card"
                    onClick={() => setPreview(item)}
                    aria-label={'查看作品：' + (item.reason || item.prompt)}
                  >
                    {urls[item.id] ? (
                      <img src={urls[item.id]} alt={item.prompt} loading="lazy" />
                    ) : (
                      <div className="art-placeholder" aria-hidden="true">加载中</div>
                    )}
                    <span className="art-overlay" aria-hidden="true">
                      <span className="art-reason">{item.reason || item.prompt}</span>
                    </span>
                  </button>
                ))}
              </div>
            ))}
          </div>
        )}
      </div>

      {preview && (
        <div
          className="scrim scrim--lightbox"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setPreview(null)
          }}
          onKeyDown={(event) => {
            if (event.key === 'Escape') setPreview(null)
          }}
        >
          <figure className="lightbox" role="dialog" aria-modal="true" aria-label="作品预览">
            {urls[preview.id] && <img src={urls[preview.id]} alt={preview.prompt} />}
            <figcaption>
              <strong>{preview.reason || '没有写理由'}</strong>
              <span>{preview.prompt}</span>
            </figcaption>
            <button type="button" className="icon-btn lightbox-close" onClick={() => setPreview(null)} aria-label="关闭预览">×</button>
          </figure>
        </div>
      )}
    </section>
  )
}
