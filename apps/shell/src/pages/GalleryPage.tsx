import { useEffect, useRef, useState } from 'react'
import type { AppError, ArtworkItem } from '../types'
import { IconClose, IconRefresh } from '../icons'

interface GalleryPageProps {
  items: ArtworkItem[]
  /** id → object URL（已经过鉴权 fetch）。 */
  urls: Record<string, string>
  loading: boolean
  error: AppError | null
  onRefresh: () => void
}

/**
 * 单张作品卡。
 *
 * **修复「图片抽搐」的关键**：
 * 1. 卡片本身用 CSS aspect-ratio: 1/1 占位，高度在图片加载前就已确定；
 * 2. 图片 absolute 定位填满卡片，完全不参与布局；
 * 3. 等待 img.decode() 成功后再加 is-loaded 类做 opacity 淡入。
 *
 * 三者合起来保证：图片从「未加载 → 解码完成」的整个过程中，
 * 卡片高度一次都不变，不会出现高度回流导致的抖动。
 * 这里**不做任何基于图片自然尺寸的高度估算**。
 */
function ArtCard({ item, url, onOpen }: { item: ArtworkItem; url?: string; onOpen: () => void }) {
  const [loaded, setLoaded] = useState(false)
  const imgRef = useRef<HTMLImageElement>(null)

  useEffect(() => {
    const img = imgRef.current
    if (!img || !url) return
    let cancelled = false

    const markLoaded = () => {
      if (!cancelled) setLoaded(true)
    }

    // 优先用 decode()：确保像素已就绪再显示，避免半张图闪出来
    if (typeof img.decode === 'function') {
      img.decode().then(markLoaded, markLoaded)
    } else if (img.complete) {
      markLoaded()
    }
    img.addEventListener('load', markLoaded, { once: true })
    return () => {
      cancelled = true
      img.removeEventListener('load', markLoaded)
    }
  }, [url])

  return (
    <button type="button" className="art" onClick={onOpen} aria-label={'查看作品：' + (item.reason || item.prompt)}>
      {!loaded && <span className="art-skeleton" aria-hidden="true" />}
      {url && (
        <img
          ref={imgRef}
          src={url}
          alt={item.prompt}
          className={loaded ? 'is-loaded' : undefined}
          loading="lazy"
          decoding="async"
        />
      )}
      <span className="art-overlay" aria-hidden="true">
        <span className="art-reason">{item.reason || item.prompt}</span>
      </span>
    </button>
  )
}

export function GalleryPage({ items, urls, loading, error, onRefresh }: GalleryPageProps) {
  const [preview, setPreview] = useState<ArtworkItem | null>(null)

  // Esc 关闭预览
  useEffect(() => {
    if (!preview) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setPreview(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [preview])

  return (
    <section className="page" aria-label="画廊">
      <div className="page-head">
        <div>
          <h2 className="page-title">画廊</h2>
          <p className="page-sub">灵鲸鲸画过的东西都在这里</p>
        </div>
        <div className="page-actions">
          <button type="button" className="btn btn--icon" onClick={onRefresh} disabled={loading} aria-label="刷新">
            <span className={loading ? 'spin' : undefined} style={{ display: 'inline-flex' }}>
              <IconRefresh size={16} />
            </span>
          </button>
        </div>
      </div>

      <div className="page-body">
        {error && (
          <div className="alert alert--error" role="alert" style={{ marginBottom: 'var(--s4)' }}>
            <div>
              <strong>{error.message}</strong>
              <span>{error.hint}</span>
            </div>
          </div>
        )}

        {loading && items.length === 0 ? (
          <div className="grid-gallery" aria-label="正在加载">
            {Array.from({ length: 8 }).map((_, i) => (
              <div key={i} className="art" aria-hidden="true">
                <span className="art-skeleton" />
              </div>
            ))}
          </div>
        ) : items.length === 0 ? (
          <div className="empty">
            <strong>画廊还空着</strong>
            <p>等灵鲸鲸画了第一张，就会出现在这里。</p>
          </div>
        ) : (
          <div className="grid-gallery">
            {items.map((item) => (
              <ArtCard
                key={item.id}
                item={item}
                url={urls[item.id]}
                onOpen={() => setPreview(item)}
              />
            ))}
          </div>
        )}
      </div>

      {preview && (
        <div
          className="lightbox-scrim"
          onClick={(event) => {
            if (event.target === event.currentTarget) setPreview(null)
          }}
        >
          <figure className="lightbox" role="dialog" aria-modal="true" aria-label="作品预览" style={{ margin: 0 }}>
            {urls[preview.id] && <img src={urls[preview.id]} alt={preview.prompt} />}
            <figcaption className="lightbox-meta">
              <strong>{preview.reason || '没有写理由'}</strong>
              <span>{preview.prompt}</span>
            </figcaption>
            <button
              type="button"
              className="icon-btn"
              onClick={() => setPreview(null)}
              aria-label="关闭预览"
              style={{ position: 'absolute', top: 10, right: 10 }}
            >
              <IconClose size={16} />
            </button>
          </figure>
        </div>
      )}
    </section>
  )
}
