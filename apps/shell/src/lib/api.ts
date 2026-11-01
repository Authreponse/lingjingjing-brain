import type {
  AppError,
  ArtworkItem,
  ChatResponse,
  DeploymentDoc,
  EmotionItem,
  HistoryMessage,
  HistoryResponse,
  InteractionKind,
  LingState,
  Provider,
  ProviderCreate,
  ProviderTestResult,
  ProviderUpdate,
  ProvidersResponse,
  RoutingUpdate,
} from '../types'

export type RawState = Record<string, unknown>

// ───────────────────────────── 连接配置 ─────────────────────────────
// 契约 §4：读取顺序 URL query > localStorage > 默认。

const DEFAULT_URL = 'http://127.0.0.1:7100'
const DEFAULT_TOKEN = 'lingjingjing-local-dev'
const URL_KEY = 'lingjingjing.shell.base-url'
const TOKEN_KEY = 'lingjingjing.shell.token'

function readQuery(name: string): string | null {
  try {
    return new URLSearchParams(window.location.search).get(name)
  } catch {
    return null
  }
}

function readStore(key: string): string | null {
  try {
    return window.localStorage.getItem(key)
  } catch {
    return null
  }
}

export function resolveConfig(): { baseUrl: string; token: string } {
  const qUrl = readQuery('LING_BRAIN_URL') ?? readQuery('base_url')
  const qToken = readQuery('LING_BRAIN_TOKEN') ?? readQuery('token')
  return {
    baseUrl: (qUrl || readStore(URL_KEY) || DEFAULT_URL).replace(/\/$/, ''),
    token: qToken || readStore(TOKEN_KEY) || DEFAULT_TOKEN,
  }
}

export function saveConfig(baseUrl: string, token: string): void {
  try {
    window.localStorage.setItem(URL_KEY, baseUrl.replace(/\/$/, ''))
    window.localStorage.setItem(TOKEN_KEY, token)
  } catch {
    // 隐私模式不可写：本次运行仍生效。
  }
}

export const DEFAULT_BASE_URL = DEFAULT_URL
export const DEFAULT_TOKEN_VALUE = DEFAULT_TOKEN

// ───────────────────────────── 错误处理 ─────────────────────────────

export class ApiError extends Error {
  readonly status?: number
  readonly detail: unknown

  constructor(status?: number, detail?: unknown) {
    super('request failed')
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/**
 * 把 422 的 detail 拆成可展示的问题列表。
 * 契约 §3：detail 可为字符串，或 {"error","problems"}。
 */
function extractProblems(detail: unknown): string[] {
  if (typeof detail === 'string') return detail.trim() ? [detail] : []
  if (!isRecord(detail)) return []
  const out: string[] = []
  if (typeof detail.error === 'string' && detail.error.trim()) out.push(detail.error)
  const problems = detail.problems
  if (Array.isArray(problems)) {
    for (const p of problems) {
      if (typeof p === 'string') out.push(p)
      else if (isRecord(p) && typeof p.message === 'string') out.push(p.message)
      else if (isRecord(p) && typeof p.msg === 'string') out.push(p.msg)
    }
  } else if (typeof problems === 'string') {
    out.push(problems)
  }
  return out
}

/** 把任意失败翻译成「说人话」的错误，绝不显示状态码或英文错误码。 */
export function toAppError(error: unknown): AppError {
  if (error instanceof ApiError) {
    const problems = extractProblems(error.detail)
    switch (error.status) {
      case 401:
      case 403:
        return {
          kind: 'auth',
          message: error.status === 403 ? '这个操作不被允许' : '连接口令不对',
          hint: error.status === 403 ? (problems[0] ?? '内置提供商不能删除') : '打开设置检查一下令牌',
        }
      case 404:
        return { kind: 'not-found', message: '没找到这个内容', hint: '可能已经被删掉了' }
      case 422:
        return {
          kind: 'validation',
          message: '填写的内容还需要调整',
          hint: problems.length > 0 ? undefined : '检查一下必填项',
          problems,
        }
      default:
        if ((error.status ?? 0) >= 500) {
          return { kind: 'server', message: '服务这边出了点状况', hint: '稍等一会再试' }
        }
        return { kind: 'unknown', message: '操作没成功', hint: '再试一次看看' }
    }
  }
  if (error instanceof Error && error.name === 'AbortError') {
    return { kind: 'timeout', message: '等太久了，先不等了', hint: '稍后再试' }
  }
  return { kind: 'offline', message: '联系不上大脑服务', hint: '确认服务开着后会自动重连' }
}

// ───────────────────────────── 请求基础设施 ─────────────────────────────

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE'
  body?: unknown
  timeoutMs?: number
  signal?: AbortSignal
}

const DEFAULT_TIMEOUT = 15_000

async function request<T>(
  path: string,
  cfg: { baseUrl: string; token: string },
  options: RequestOptions = {},
): Promise<T> {
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), options.timeoutMs ?? DEFAULT_TIMEOUT)
  // 外部 signal 也取消内部控制器
  options.signal?.addEventListener('abort', () => controller.abort(), { once: true })

  try {
    const response = await fetch(cfg.baseUrl + path, {
      method: options.method ?? 'GET',
      headers: {
        'Content-Type': 'application/json',
        'X-Ling-Token': cfg.token,
      },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: controller.signal,
    })

    const text = await response.text()
    let parsed: unknown = null
    if (text) {
      try {
        parsed = JSON.parse(text)
      } catch {
        parsed = text
      }
    }
    if (!response.ok) throw new ApiError(response.status, parsed)
    return parsed as T
  } catch (error) {
    if (error instanceof ApiError) throw error
    if (error instanceof Error && error.name === 'AbortError') {
      // 外部主动取消 vs 超时：外部取消时重新抛出，供调用方忽略
      if (options.signal?.aborted) throw error
      throw new ApiError(undefined, { timeout: true })
    }
    throw new ApiError(undefined, { offline: true })
  } finally {
    clearTimeout(timeout)
  }
}

// ───────────────────────────── 契约 1：聊天历史 ─────────────────────────────

/** GET /v1/chat/history?limit&before — 返回按时间正序的消息。 */
export async function fetchHistory(
  cfg: { baseUrl: string; token: string },
  limit = 200,
  before?: string,
  signal?: AbortSignal,
): Promise<HistoryResponse> {
  const params = new URLSearchParams({ limit: String(limit) })
  if (before) params.set('before', before)
  const raw = await request<Partial<HistoryResponse>>('/v1/chat/history?' + params.toString(), cfg, { signal })
  const items = Array.isArray(raw?.items) ? raw.items : []
  return {
    items: items.filter(isHistoryMessage),
    has_more: Boolean(raw?.has_more),
  }
}

function isHistoryMessage(value: unknown): value is HistoryMessage {
  if (!isRecord(value)) return false
  return (
    typeof value.id === 'string' &&
    typeof value.ts === 'string' &&
    (value.role === 'user' || value.role === 'assistant') &&
    typeof value.content === 'string'
  )
}

// ───────────────────────────── 既有接口 ─────────────────────────────

export async function checkHealth(cfg: { baseUrl: string; token: string }): Promise<void> {
  await request<unknown>('/v1/health', cfg, { timeoutMs: 5000 })
}

export async function fetchState(cfg: { baseUrl: string; token: string }): Promise<LingState> {
  return normalizeState(await request<RawState>('/v1/state', cfg))
}

export async function sendChat(
  cfg: { baseUrl: string; token: string },
  message: string,
  clientId: string,
  signal?: AbortSignal,
): Promise<ChatResponse> {
  return request<ChatResponse>('/v1/chat', cfg, {
    method: 'POST',
    body: { message, client_id: clientId },
    timeoutMs: 120_000,
    signal,
  })
}

let interactionSupported: boolean | undefined

export async function postInteraction(
  cfg: { baseUrl: string; token: string },
  kind: InteractionKind,
  clientId: string,
): Promise<LingState | null> {
  if (interactionSupported === false) return null
  try {
    const raw = await request<RawState>('/v1/state/interact', cfg, {
      method: 'POST',
      body: { kind, client_id: clientId, count: 1 },
      timeoutMs: 8000,
    })
    interactionSupported = true
    return normalizeState(raw)
  } catch (error) {
    if (error instanceof ApiError && (error.status ?? 0) >= 400 && (error.status ?? 0) < 500) {
      interactionSupported = false
    }
    return null
  }
}

export async function listArtwork(cfg: { baseUrl: string; token: string }, limit = 60): Promise<ArtworkItem[]> {
  const raw = await request<{ items?: ArtworkItem[] }>('/v1/artwork?limit=' + limit, cfg)
  return Array.isArray(raw?.items) ? raw.items : []
}

export async function fetchEmotions(cfg: { baseUrl: string; token: string }): Promise<EmotionItem[]> {
  const raw = await request<{ items?: EmotionItem[] }>('/v1/emotions', cfg)
  return Array.isArray(raw?.items) ? raw.items : []
}

/** 作品图片需要鉴权头，<img src> 带不了，必须 fetch 成 blob。 */
export async function fetchArtworkBlob(
  cfg: { baseUrl: string; token: string },
  artworkId: string,
): Promise<Blob | null> {
  if (!artworkId) return null
  try {
    const response = await fetch(
      cfg.baseUrl + '/v1/artwork/' + encodeURIComponent(artworkId) + '/file',
      { headers: { 'X-Ling-Token': cfg.token } },
    )
    if (!response.ok) return null
    return await response.blob()
  } catch {
    return null
  }
}

// ─────────────────────────── 契约 2：模型提供商 ───────────────────────────

export async function listProviders(cfg: { baseUrl: string; token: string }): Promise<ProvidersResponse> {
  const raw = await request<Partial<ProvidersResponse>>('/v1/providers', cfg)
  return {
    items: Array.isArray(raw?.items) ? (raw.items as Provider[]) : [],
    routing: {
      mode: raw?.routing?.mode === 'concurrent' ? 'concurrent' : 'single',
      active_provider_id: raw?.routing?.active_provider_id ?? null,
      enabled_ids: Array.isArray(raw?.routing?.enabled_ids) ? raw.routing.enabled_ids : [],
      assignments: isRecord(raw?.routing?.assignments)
        ? (raw.routing.assignments as Record<string, string>)
        : {},
    },
    limits: { max_enabled: raw?.limits?.max_enabled ?? 4 },
  }
}

export async function createProvider(
  cfg: { baseUrl: string; token: string },
  body: ProviderCreate,
): Promise<Provider> {
  return request<Provider>('/v1/providers', cfg, { method: 'POST', body })
}

/**
 * PUT /v1/providers/{id}
 * api_key 语义（契约）：不传 = 不修改；"" = 不修改；null = 清除。
 * 这里按「只发送调用方确实指定的键」实现，避免把 api_key 意外发成 ""。
 */
export async function updateProvider(
  cfg: { baseUrl: string; token: string },
  id: string,
  patch: ProviderUpdate,
): Promise<Provider> {
  const body: Record<string, unknown> = {}
  for (const key of ['name', 'kind', 'base_url', 'model', 'capabilities'] as const) {
    if (patch[key] !== undefined) body[key] = patch[key]
  }
  if (patch.api_key !== undefined) body.api_key = patch.api_key
  return request<Provider>('/v1/providers/' + encodeURIComponent(id), cfg, { method: 'PUT', body })
}

export async function deleteProvider(
  cfg: { baseUrl: string; token: string },
  id: string,
): Promise<void> {
  await request<unknown>('/v1/providers/' + encodeURIComponent(id), cfg, { method: 'DELETE' })
}

export async function testProvider(
  cfg: { baseUrl: string; token: string },
  id: string,
): Promise<ProviderTestResult> {
  const raw = await request<Partial<ProviderTestResult>>(
    '/v1/providers/' + encodeURIComponent(id) + '/test',
    cfg,
    { method: 'POST', timeoutMs: 12_000 },
  )
  return {
    ok: Boolean(raw?.ok),
    latency_ms: typeof raw?.latency_ms === 'number' ? raw.latency_ms : 0,
    models: Array.isArray(raw?.models) ? raw.models.map(String) : [],
    error: typeof raw?.error === 'string' ? raw.error : null,
  }
}

export async function updateRouting(
  cfg: { baseUrl: string; token: string },
  body: RoutingUpdate,
): Promise<unknown> {
  return request<unknown>('/v1/providers/routing', cfg, { method: 'POST', body })
}

// ───────────────────── 契约：部署配置（task-22） ─────────────────────

/** GET /v1/deployment —— 读取持久化在服务器端的部署配置。 */
export async function fetchDeployment(
  cfg: { baseUrl: string; token: string },
): Promise<DeploymentDoc> {
  const raw = await request<Partial<DeploymentDoc>>('/v1/deployment', cfg, { timeoutMs: 8000 })
  return normalizeDeployment(raw)
}

/** PUT /v1/deployment —— 写回部署配置；422 表示校验失败。 */
export async function putDeployment(
  cfg: { baseUrl: string; token: string },
  doc: DeploymentDoc,
): Promise<DeploymentDoc> {
  const raw = await request<Partial<DeploymentDoc>>('/v1/deployment', cfg, {
    method: 'PUT',
    body: doc,
    timeoutMs: 8000,
  })
  return normalizeDeployment(raw ?? doc)
}

export function normalizeDeployment(raw: unknown): DeploymentDoc {
  const rec = isRecord(raw) ? raw : {}
  const mode = rec.mode === 'terminal' || rec.mode === 'server' ? rec.mode : 'local'
  return {
    mode,
    server_url: typeof rec.server_url === 'string' ? rec.server_url : '',
    token: typeof rec.token === 'string' ? rec.token : '',
  }
}

// ───────────────────────────── 状态归一化 ─────────────────────────────

const PERCENT_KEYS = ['percent', 'value', 'pct', 'level', 'score', 'ratio'] as const

/** 数字/数字串/0–1 小数/包装对象 → 0–100；识别不了返回 undefined，绝不臆造 0。 */
export function normalizePercent(value: unknown): number | undefined {
  let candidate: unknown = value
  if (isRecord(candidate)) {
    const boxed = candidate
    candidate = PERCENT_KEYS.map((k) => boxed[k]).find((v) => v !== undefined && v !== null)
  }
  if (typeof candidate === 'string') {
    const trimmed = candidate.trim().replace(/%$/, '')
    candidate = trimmed === '' ? Number.NaN : Number(trimmed)
  }
  if (typeof candidate !== 'number' || !Number.isFinite(candidate)) return undefined
  const scaled = candidate >= 0 && candidate <= 1 ? candidate * 100 : candidate
  return Math.round(Math.min(100, Math.max(0, scaled)))
}

export function normalizeFlag(value: unknown): boolean | undefined {
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') return Number.isFinite(value) ? value !== 0 : undefined
  if (typeof value === 'string') {
    const lowered = value.trim().toLowerCase()
    if (['true', '1', 'on', 'yes', '是'].includes(lowered)) return true
    if (['false', '0', 'off', 'no', '否'].includes(lowered)) return false
  }
  return undefined
}

export function normalizeState(payload: unknown): LingState {
  const wrapped = isRecord(payload) && isRecord(payload.state) ? payload.state : undefined
  const source: RawState = wrapped ?? (isRecord(payload) ? payload : {})
  const label = (v: unknown) => (typeof v === 'string' && v.trim() ? v.trim() : undefined)

  const state: LingState = {
    energy: normalizePercent(source.energy),
    satiety: normalizePercent(source.satiety),
    tiredness: normalizePercent(source.fatigue) ?? normalizePercent(source.tiredness),
    mood: label(source.mood),
    presence: label(source.presence),
    quiet: normalizeFlag(source.in_quiet_hours) ?? normalizeFlag(source.quiet),
    affection: normalizePercent(source.affection),
    hungry: normalizeFlag(source.hungry),
    tired: normalizeFlag(source.tired),
  }
  return Object.fromEntries(Object.entries(state).filter(([, v]) => v !== undefined)) as LingState
}

export function hasUsableState(state: LingState): boolean {
  return Object.values(state).some((v) => v !== undefined)
}
