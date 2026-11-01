import { useCallback, useEffect, useState } from 'react'
import type { Deployment, DeploymentMode, DeploymentSync } from '../lib/storage'
import { DEFAULT_NAME } from '../lib/storage'
import { ModelsSection } from './ModelsSection'
import { IconBolt, IconCheck } from '../icons'

interface SettingsPageProps {
  cfg: { baseUrl: string; token: string }
  whaleName: string
  deployment: Deployment | null
  /** 部署配置是否已写入后端 /v1/deployment；cached 表示仅存本地，需在 UI 标注降级。 */
  deploymentSync: DeploymentSync
  onSaveName: (name: string) => void
  onSaveDeployment: (dep: Deployment) => void
  onSaveConnection: (baseUrl: string, token: string) => void
  onToast: (tone: 'info' | 'success' | 'error', message: string) => void
}

type Section = 'general' | 'models' | 'about'

const MODE_LABEL: Record<DeploymentMode, string> = {
  local: '本地部署',
  terminal: '终端',
  server: '服务器',
}

/**
 * 设置页。依据线框：名字 / 令牌 / 外观 / 部署模式 / 关于。
 * 「模型」提供商配置并入本页（原 ModelsPage → ModelsSection）。
 */
export function SettingsPage({
  cfg, whaleName, deployment, deploymentSync, onSaveName, onSaveDeployment, onSaveConnection, onToast,
}: SettingsPageProps) {
  const [section, setSection] = useState<Section>('general')
  const [name, setName] = useState(whaleName)
  const [url, setUrl] = useState(cfg.baseUrl)
  const [token, setToken] = useState(cfg.token)
  const [mode, setMode] = useState<DeploymentMode>(deployment?.mode ?? 'local')
  const [serverUrl, setServerUrl] = useState(deployment?.server_url ?? '')
  const [serverToken, setServerToken] = useState(deployment?.token ?? '')
  const [saved, setSaved] = useState(false)

  useEffect(() => { setName(whaleName) }, [whaleName])
  useEffect(() => { setUrl(cfg.baseUrl); setToken(cfg.token) }, [cfg])

  const flash = useCallback(() => {
    setSaved(true)
    window.setTimeout(() => setSaved(false), 1800)
  }, [])

  const saveGeneral = () => {
    onSaveName(name.trim() || DEFAULT_NAME)
    // 部署模式与连接参数一起保存，避免「模式说连 A、实际连 B」
    onSaveDeployment({
      mode,
      server_url: mode === 'server' ? serverUrl.trim() : '',
      token: mode === 'server' ? serverToken.trim() : token,
    })
    if (mode !== 'server') onSaveConnection(url.trim(), token.trim())
    flash()
    onToast('success', '已保存')
  }

  return (
    <section className="page" aria-label="设置">
      <div className="page-head">
        <div>
          <h2 className="page-title">设置</h2>
          <p className="page-sub">名字、连接、模型，都在这里调</p>
        </div>
      </div>

      <div className="page-body">
        <div className="segmented settings-tabs" role="tablist" aria-label="设置分区">
          <button type="button" role="tab" aria-selected={section === 'general'}
            className={section === 'general' ? 'is-on' : undefined} onClick={() => setSection('general')}>
            通用
          </button>
          <button type="button" role="tab" aria-selected={section === 'models'}
            className={section === 'models' ? 'is-on' : undefined} onClick={() => setSection('models')}>
            模型
          </button>
          <button type="button" role="tab" aria-selected={section === 'about'}
            className={section === 'about' ? 'is-on' : undefined} onClick={() => setSection('about')}>
            关于
          </button>
        </div>

        {section === 'general' && (
          <div className="settings-block">
            <h3 className="settings-h3">鲸鱼名字</h3>
            <div className="field">
              <label htmlFor="set-name">她现在叫什么</label>
              <input id="set-name" className="input" value={name} maxLength={20}
                onChange={(e) => setName(e.target.value)} placeholder={DEFAULT_NAME} />
              <p className="field-help">顶栏会显示这个名字。</p>
            </div>

            <h3 className="settings-h3">部署模式</h3>
            <div className="mode-row" role="radiogroup" aria-label="部署模式">
              {(['local', 'terminal', 'server'] as DeploymentMode[]).map((m) => (
                <button
                  key={m}
                  type="button"
                  role="radio"
                  aria-checked={mode === m}
                  className={'mode-card' + (mode === m ? ' is-on' : '')}
                  onClick={() => setMode(m)}
                >
                  <span className="mode-name">
                    {mode === m && <IconCheck size={13} />}
                    {MODE_LABEL[m]}
                  </span>
                  <span className="mode-desc">
                    {m === 'local' ? '在这台电脑上运行' : m === 'terminal' ? '本机终端里的后端' : '远程服务器'}
                  </span>
                </button>
              ))}
            </div>

            {/* 降级说明：不隐藏「只存在本地」这个事实 */}
            {deploymentSync === 'cached' && (
              <div className="alert alert--warn" role="status">
                <div>
                  <strong>部署配置只保存在本机</strong>
                  <span>
                    后端还没提供 /v1/deployment，所以这次只在本地记住。
                    换成能连通的后端后会自动同步。
                  </span>
                </div>
              </div>
            )}
            {deploymentSync === 'synced' && (
              <p className="field-help" style={{ color: 'var(--mint)' }}>已同步到服务器（/v1/deployment）</p>
            )}

            {mode === 'server' ? (
              <>
                <div className="field">
                  <label htmlFor="set-server">服务器地址</label>
                  <input id="set-server" className="input" value={serverUrl} spellCheck={false}
                    onChange={(e) => setServerUrl(e.target.value)} placeholder="https://ling.example.com" />
                </div>
                <div className="field">
                  <label htmlFor="set-server-token">服务器口令</label>
                  <input id="set-server-token" className="input" type="password" value={serverToken}
                    autoComplete="off" onChange={(e) => setServerToken(e.target.value)} />
                </div>
              </>
            ) : (
              <>
                <div className="field">
                  <label htmlFor="set-url">服务地址</label>
                  <input id="set-url" className="input" value={url} spellCheck={false}
                    onChange={(e) => setUrl(e.target.value)} placeholder="http://127.0.0.1:7100" />
                </div>
                <div className="field">
                  <label htmlFor="set-token">口令</label>
                  <input id="set-token" className="input" type="password" value={token}
                    autoComplete="off" onChange={(e) => setToken(e.target.value)} />
                  <p className="field-help">只保存在这台设备上。</p>
                </div>
              </>
            )}

            <div className="settings-actions">
              <button type="button" className="btn btn--primary" onClick={saveGeneral}>
                {saved ? <><IconCheck size={15} /> 已保存</> : '保存'}
              </button>
            </div>
          </div>
        )}

        {section === 'models' && <ModelsSection cfg={cfg} onToast={onToast} />}

        {section === 'about' && (
          <div className="settings-block">
            <h3 className="settings-h3">关于</h3>
            <p className="field-help" style={{ lineHeight: 1.8 }}>
              灵鲸鲸是一个跑在你自己机器上的 AI 个体。她的记忆、状态与作品都保存在本地。
              <br />
              当前连接：{cfg.baseUrl}
            </p>
            <div className="about-facts">
              <div><span>版本</span><strong>v4.0 shell</strong></div>
              <div><span>部署模式</span><strong>{MODE_LABEL[deployment?.mode ?? 'local']}</strong></div>
            </div>
            <p className="field-help" style={{ marginTop: 'var(--s4)' }}>
              <IconBolt size={12} /> 桌面外壳的透明窗口与托盘能力尚未在本机实机验证。
            </p>
          </div>
        )}
      </div>
    </section>
  )
}
