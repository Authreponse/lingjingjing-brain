import { useState } from 'react'
import type { Deployment, DeploymentMode } from '../lib/storage'
import { DEFAULT_TOKEN } from '../lib/storage'
import { IconCheck, IconWhaleMark } from '../icons'

interface OnboardingProps {
  onDone: (dep: Deployment) => void
}

type Step = 'has-server' | 'server-kind' | 'server-config'

const MODE_LABEL: Record<DeploymentMode, string> = {
  local: '本地部署',
  terminal: '终端',
  server: '服务器',
}

/**
 * 首次启动引导（三步单选，风格与 splash 一致）。
 * 依据 docs/ui-v4-layout.md「首次启动流程」。
 *
 * 流程：1) 是否有服务器部署 → 2) 有则区分终端/服务器 → 3) 服务器模式填 base_url + token。
 * 结果写 localStorage；data/deployment.json 的落盘由 launcher/后端负责（前端不直接写文件）。
 */
export function Onboarding({ onDone }: OnboardingProps) {
  const [step, setStep] = useState<Step>('has-server')

  const [url, setUrl] = useState('')
  const [token, setToken] = useState('')
  const [error, setError] = useState('')

  const finish = (mode: DeploymentMode, serverUrl = '', serverToken = '') => {
    onDone({ mode, server_url: serverUrl, token: serverToken })
  }

  const chooseNoServer = () => finish('local')

  const chooseHasServer = () => setStep('server-kind')

  const chooseKind = (mode: DeploymentMode) => {
    if (mode === 'terminal') {
      // 终端模式：launcher 已在本机拉起后端，直接用默认地址
      finish('terminal')
    } else {
      setStep('server-config')
    }
  }

  const submitServer = () => {
    const trimmed = url.trim()
    if (!/^https?:\/\//i.test(trimmed)) {
      setError('请填写以 http:// 或 https:// 开头的地址')
      return
    }
    setError('')
    finish('server', trimmed, token.trim() || DEFAULT_TOKEN)
  }

  return (
    <div className="onboard" role="dialog" aria-modal="true" aria-label="首次设置">
      <div className="onboard-card">
        <div className="onboard-head">
          <IconWhaleMark size={44} />
          <h1>先认识一下</h1>
          <p>灵鲸鲸需要知道去哪里找你</p>
        </div>

        <ol className="onboard-steps" aria-hidden="true">
          <li className={step === 'has-server' ? 'is-on' : 'is-done'} />
          <li className={step === 'server-kind' ? 'is-on' : step === 'server-config' ? 'is-done' : ''} />
          <li className={step === 'server-config' ? 'is-on' : ''} />
        </ol>

        {step === 'has-server' && (
          <>
            <p className="onboard-q">你有自己的服务器吗？</p>
            <div className="onboard-options">
              <button type="button" className="onboard-option" onClick={chooseNoServer}>
                <strong>没有</strong>
                <span>就在这台电脑上运行，我会自动帮你启动</span>
              </button>
              <button type="button" className="onboard-option" onClick={chooseHasServer}>
                <strong>有</strong>
                <span>连接到已经部署好的灵鲸鲸</span>
              </button>
            </div>
          </>
        )}

        {step === 'server-kind' && (
          <>
            <p className="onboard-q">是哪一种？</p>
            <div className="onboard-options">
              <button type="button" className="onboard-option" onClick={() => chooseKind('terminal')}>
                <strong>终端</strong>
                <span>后端跑在本机终端里，用默认地址连接</span>
              </button>
              <button type="button" className="onboard-option" onClick={() => chooseKind('server')}>
                <strong>服务器</strong>
                <span>远程部署，需要填地址和口令</span>
              </button>
            </div>
            <button type="button" className="btn btn--ghost onboard-back" onClick={() => setStep('has-server')}>
              返回
            </button>
          </>
        )}

        {step === 'server-config' && (
          <>
            <p className="onboard-q">填一下服务器信息</p>
            <div className="field">
              <label htmlFor="ob-url">服务地址</label>
              <input
                id="ob-url"
                className="input"
                value={url}
                spellCheck={false}
                placeholder="https://ling.example.com"
                onChange={(e) => setUrl(e.target.value)}
                autoFocus
              />
            </div>
            <div className="field">
              <label htmlFor="ob-token">口令</label>
              <input
                id="ob-token"
                className="input"
                type="password"
                value={token}
                autoComplete="off"
                placeholder="留空使用默认开发口令"
                onChange={(e) => setToken(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') submitServer() }}
              />
            </div>
            {error && <p className="onboard-error" role="alert">{error}</p>}
            <div className="onboard-actions">
              <button type="button" className="btn btn--ghost" onClick={() => setStep('server-kind')}>返回</button>
              <button type="button" className="btn btn--primary" onClick={submitServer}>
                <IconCheck size={15} />
                开始
              </button>
            </div>
          </>
        )}

        <p className="onboard-foot">之后可以在「设置」里随时修改（{MODE_LABEL.local} / {MODE_LABEL.terminal} / {MODE_LABEL.server}）</p>
      </div>
    </div>
  )
}
