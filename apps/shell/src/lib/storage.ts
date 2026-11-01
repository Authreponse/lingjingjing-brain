/** 部署模式（docs/ui-v4-layout.md 首次启动流程）。 */
export type DeploymentMode = 'local' | 'terminal' | 'server'

export interface Deployment {
  mode: DeploymentMode
  server_url: string
  token: string
}

const DEPLOY_KEY = 'lingjingjing.shell.deployment'
const URL_KEY = 'lingjingjing.shell.base-url'
const TOKEN_KEY = 'lingjingjing.shell.token'
const NAME_KEY = 'lingjingjing.shell.whale-name'
const ONBOARDED_KEY = 'lingjingjing.shell.onboarded'

export const DEFAULT_BASE_URL = 'http://127.0.0.1:7100'
export const DEFAULT_TOKEN = 'lingjingjing-local-dev'
export const DEFAULT_NAME = '灵鲸鲸'

function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key)
  } catch {
    return null
  }
}

function write(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value)
  } catch {
    // 隐私模式：本次运行内仍生效
  }
}

function readQuery(name: string): string | null {
  try {
    return new URLSearchParams(window.location.search).get(name)
  } catch {
    return null
  }
}

/** 是否已完成首次引导。 */
export function isOnboarded(): boolean {
  return read(ONBOARDED_KEY) === '1'
}

export function markOnboarded(): void {
  write(ONBOARDED_KEY, '1')
}

export function loadDeployment(): Deployment | null {
  const raw = read(DEPLOY_KEY)
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw) as Partial<Deployment>
    const mode: DeploymentMode =
      parsed.mode === 'terminal' || parsed.mode === 'server' ? parsed.mode : 'local'
    return {
      mode,
      server_url: typeof parsed.server_url === 'string' ? parsed.server_url : '',
      token: typeof parsed.token === 'string' ? parsed.token : '',
    }
  } catch {
    return null
  }
}

/**
 * 部署配置的持久化策略（task-22 授权后调整）：
 *
 * - **后端 /v1/deployment 是主存储**（GET/PUT，契约见 lead 下达）；
 * - localStorage 只作为「端点不可用时的缓存兜底」；
 * - 通过 `DeploymentSync` 告诉 UI 当前处于哪种状态，UI 必须**明确标注降级**，
 *   不能把「只存在本地」当成「已经同步到服务器」。
 */
export type DeploymentSync = 'synced' | 'cached' | 'unknown'

export function saveDeployment(dep: Deployment): void {
  write(DEPLOY_KEY, JSON.stringify(dep))
  // 同步到连接配置，保证「部署模式」和「实际连到哪里」永远一致
  if (dep.mode === 'server' && dep.server_url) {
    write(URL_KEY, dep.server_url.replace(/\/$/, ''))
    write(TOKEN_KEY, dep.token)
  } else {
    write(URL_KEY, DEFAULT_BASE_URL)
    write(TOKEN_KEY, dep.token || DEFAULT_TOKEN)
  }
}

export function loadWhaleName(): string {
  return read(NAME_KEY) || DEFAULT_NAME
}

export function saveWhaleName(name: string): void {
  write(NAME_KEY, name.trim() || DEFAULT_NAME)
}

/**
 * 解析实际连接参数。契约 §4：URL query > localStorage > 默认。
 * 部署模式为 server 时，优先用其保存的地址与令牌。
 */
export function resolveConnection(): { baseUrl: string; token: string } {
  const queryUrl = readQuery('LING_BRAIN_URL') ?? readQuery('base_url')
  const queryToken = readQuery('LING_BRAIN_TOKEN') ?? readQuery('token')
  if (queryUrl || queryToken) {
    return {
      baseUrl: (queryUrl || read(URL_KEY) || DEFAULT_BASE_URL).replace(/\/$/, ''),
      token: queryToken || read(TOKEN_KEY) || DEFAULT_TOKEN,
    }
  }

  const dep = loadDeployment()
  if (dep?.mode === 'server' && dep.server_url.trim()) {
    return {
      baseUrl: dep.server_url.trim().replace(/\/$/, ''),
      token: dep.token || DEFAULT_TOKEN,
    }
  }

  return {
    baseUrl: (read(URL_KEY) || DEFAULT_BASE_URL).replace(/\/$/, ''),
    token: read(TOKEN_KEY) || DEFAULT_TOKEN,
  }
}

export function saveConnection(baseUrl: string, token: string): void {
  write(URL_KEY, baseUrl.replace(/\/$/, ''))
  write(TOKEN_KEY, token)
}

