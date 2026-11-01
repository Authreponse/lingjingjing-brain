import type { ReactElement } from 'react'
import type { AppError } from '../types'
import { IconBolt, IconGallery, IconModels, IconWave } from '../icons'

interface ToolsPageProps {
  onGoGallery: () => void
  onGoModels: () => void
  onGoInteract: () => void
  error: AppError | null
}

interface Tool {
  key: string
  title: string
  desc: string
  icon: (p: { size?: number }) => ReactElement
  action: () => void
  ready: boolean
}

/**
 * 「小鲸鱼的工具」：能力工具箱。
 *
 * 依据线框：表情渲染、生图快捷入口、语音入口。
 * 现阶段只有「生图（画廊）」与「模型配置」是真正接通的；
 * 表情与语音**尚未接入本壳**，因此如实标注为「即将开放」，
 * 不做成可点却不生效的假按钮（原则：不隐藏降级与失败）。
 */
export function ToolsPage({ onGoGallery, onGoModels, onGoInteract, error }: ToolsPageProps) {
  const tools: Tool[] = [
    {
      key: 'image',
      title: '画一张',
      desc: '让灵鲸鲸画点东西，作品会收进历史画作',
      icon: IconGallery,
      action: onGoGallery,
      ready: true,
    },
    {
      key: 'models',
      title: '模型配置',
      desc: '换一个模型来思考',
      icon: IconModels,
      action: onGoModels,
      ready: true,
    },
    {
      key: 'interact',
      title: '互动',
      desc: '摸摸、投喂、一起玩',
      icon: IconBolt,
      action: onGoInteract,
      ready: true,
    },
    {
      key: 'emotion',
      title: '表情包',
      desc: '按心情生成表情图（等待后端接口接入本壳）',
      icon: IconWave,
      action: () => undefined,
      ready: false,
    },
    {
      key: 'speech',
      title: '语音',
      desc: '按住说话、朗读回复（等待接口接入本壳）',
      icon: IconWave,
      action: () => undefined,
      ready: false,
    },
  ]

  return (
    <section className="page" aria-label="小鲸鱼的工具">
      <div className="page-head">
        <div>
          <h2 className="page-title">小鲸鱼的工具</h2>
          <p className="page-sub">她会的东西都放在这里</p>
        </div>
      </div>

      <div className="page-body">
        {error && (
          <div className="alert alert--error" role="alert" style={{ marginBottom: 'var(--s4)' }}>
            <div><strong>{error.message}</strong><span>{error.hint}</span></div>
          </div>
        )}

        <div className="tool-grid">
          {tools.map((t) => {
            const Icon = t.icon
            return (
              <button
                key={t.key}
                type="button"
                className={'tool-card' + (t.ready ? '' : ' tool-card--soon')}
                onClick={t.ready ? t.action : undefined}
                disabled={!t.ready}
                aria-disabled={!t.ready}
              >
                <span className="tool-icon"><Icon size={22} /></span>
                <span className="tool-title">
                  {t.title}
                  {!t.ready && <span className="chip">即将开放</span>}
                </span>
                <span className="tool-desc">{t.desc}</span>
              </button>
            )
          })}
        </div>
      </div>
    </section>
  )
}
