import type { EmotionItem, EmotionRecommendation, RenderResult } from '../types'

interface MoodPanelProps {
  emotions: EmotionItem[]
  recommended: EmotionRecommendation
  loading: boolean
  rendering: string | null
  result: RenderResult | null
  resultUrl: string | null
  error: { message: string; hint?: string } | null
  onRenderSelf: () => void
  onRenderEmotion: (id: string) => void
  onArtwork: () => void
}

/**
 * 心情 / 表情面板。
 * 后端把降级情况放在 result.degraded + result.error 里，这里如实展示，
 * 不假装画好了（原则：不隐藏降级）。
 */
export function MoodPanel({
  emotions,
  recommended,
  loading,
  rendering,
  result,
  resultUrl,
  error,
  onRenderSelf,
  onRenderEmotion,
  onArtwork,
}: MoodPanelProps) {
  const selfLoading = rendering === 'self'

  return (
    <section className="panel" aria-label="心情与表情">
      <div className="panel-head">
        <div>
          <h2 className="panel-title">心情</h2>
          <p className="panel-sub">让我画张表情给你</p>
        </div>
        <button
          type="button"
          className="btn btn--primary"
          onClick={onRenderSelf}
          disabled={rendering !== null}
        >
          {selfLoading ? '正在画…' : '画个你自己'}
        </button>
      </div>

      <div className="panel-body">
        {error && (
          <div className="alert alert--error" role="alert" style={{ marginBottom: 16 }}>
            <div>
              <strong>{error.message}</strong>
              {error.hint && <span>{error.hint}</span>}
            </div>
          </div>
        )}

        {result && (
          <div className="result-card">
            {resultUrl ? (
              <img className="result-image" src={resultUrl} alt={result.text} />
            ) : (
              <div style={{ minWidth: 0 }}>
                <div className="alert alert--info">
                  <div>
                    <strong>{result.text}</strong>
                    <span>
                      {result.degraded
                        ? result.error ?? '这次只给了文字表情'
                        : '图片还在路上，稍后到画廊看'}
                    </span>
                  </div>
                </div>
              </div>
            )}
            <div className="result-copy">
              <strong>{result.emotion?.name ?? '灵鲸鲸的表情'}</strong>
              <p>{result.degraded ? '这次是降级结果' : '画好了'}</p>
              <button type="button" className="btn" onClick={onArtwork}>去画廊看看</button>
            </div>
          </div>
        )}

        {loading && emotions.length === 0 ? (
          <p className="panel-sub">正在取表情列表…</p>
        ) : emotions.length === 0 ? (
          <div className="empty">
            <strong>还没有可用的表情</strong>
            <p>可以先点右上角「画个你自己」试试</p>
          </div>
        ) : (
          <>
            {recommended.emotion && (
              <p className="panel-sub" style={{ marginBottom: 14 }}>
                现在最适合的是「{recommended.emotion.name}」
              </p>
            )}
            <div className="emotion-grid">
              {emotions.map((item) => {
                const isPick = recommended.emotion?.id === item.id
                const busy = rendering === item.id
                return (
                  <button
                    key={item.id}
                    type="button"
                    className={isPick ? 'emotion-card is-pick' : 'emotion-card'}
                    onClick={() => onRenderEmotion(item.id)}
                    disabled={rendering !== null}
                    title={item.description}
                  >
                    <span className="emotion-emoji" aria-hidden="true">{item.emoji}</span>
                    <span className="emotion-name">{item.name}</span>
                    <span className="emotion-desc">{busy ? '画着…' : item.description}</span>
                  </button>
                )
              })}
            </div>
          </>
        )}
      </div>
    </section>
  )
}
