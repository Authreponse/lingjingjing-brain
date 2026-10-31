import { invoke, isTauri } from '@tauri-apps/api/core'
import type {
  ArtworkItem,
  ChatResponse,
  EmotionItem,
  EmotionRecommendation,
  EmotionsResponse,
  FriendlyError,
  ImageGenerateResponse,
  InteractionKind,
  LingState,
  RawState,
  RenderResult,
} from './types'

export const API_BASE_URL = 'http://127.0.0.1:7100'
export const DEFAULT_TOKEN = 'lingjingjing-local-dev'

/** 普通请求 12 秒；生图要等 GPU 排队，给足 3 分钟。 */
const REQUEST_TIMEOUT_MS = 12_000
const IMAGE_TIMEOUT_MS = 180_000

export type StatePayload = RawState | { state: RawState } | null | undefined

/** 内部错误载体：带 HTTP 状态与后端 detail，供上层翻译成人话。 */
export class ApiError extends Error {
  readonly status?: number
  readonly detail: unknown

  constructor(message: string, status?: number, detail?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** 从后端错误体里挖出机器可读的 error 码，例如 image_budget_exhausted。 */
function errorCode(detail: unknown): string | undefined {
  if (!isRecord(detail)) return undefined
  const nested = isRecord(detail.detail) ? detail.detail : detail
  const code = nested.error ?? nested.code
  return typeof code === 'string' ? code : undefined
}

/**
 * 把任何失败翻译成「说人话」的错误。
 *
 * 关键点：**绝不把 HTTP 状态码或后端英文详情直接甩给用户**。
 * 用户只关心「发生了什么、我该怎么办」。
 */
export function toFriendlyError(error: unknown): FriendlyError {
  if (error instanceof ApiError) {
    const code = errorCode(error.detail)
    if (code === 'image_budget_exhausted') {
      return { kind: 'budget', message: '今天的画已经画完啦', hint: '明天再来找我画吧' }
    }
    if (code === 'gpu_queue_paused') {
      return { kind: 'busy', message: '显卡暂时歇着了', hint: '连续出错后会自动保护，稍后可在状态页恢复' }
    }
    switch (error.status) {
      case 401:
      case 403:
        return { kind: 'auth', message: '连接口令不对', hint: '打开设置，检查一下令牌' }
      case 404:
        return { kind: 'not-found', message: '没找到这个内容', hint: '可能已经不在啦' }
      case 422:
        return { kind: 'bad-request', message: '这次请求我没看懂', hint: '换个说法再试试' }
      case 429:
        return { kind: 'busy', message: '有点忙不过来了', hint: '缓一缓再叫我' }
    }
    if ((error.status ?? 0) >= 500) {
      return { kind: 'server', message: '我这边出了点小状况', hint: '稍等一下再试' }
    }
    if (error.status === undefined) {
      // 没有状态码 = 网络层失败
      return { kind: 'offline', message: '联系不上我', hint: '确认大脑服务开着了没' }
    }
  }
  if (error instanceof Error && error.name === 'AbortError') {
    return { kind: 'timeout', message: '等太久了，先不等了', hint: '网络或显卡比较慢，稍后再试' }
  }
  return { kind: 'unknown', message: '出了点小问题', hint: '再试一次看看' }
}

async function request<T>(
  path: string,
  token: string,
  init: { method?: 'GET' | 'POST'; body?: string; timeoutMs?: number } = {},
): Promise<T> {
  const controller = new AbortController()
  // 用全局 setTimeout（浏览器与 Node 测试环境都有），避免依赖 window。
  const timeout = setTimeout(() => controller.abort(), init.timeoutMs ?? REQUEST_TIMEOUT_MS)

  try {
    const response = await fetch(API_BASE_URL + path, {
      method: init.method ?? 'GET',
      headers: {
        'Content-Type': 'application/json',
        'X-Ling-Token': token,
      },
      body: init.body,
      signal: controller.signal,
    })

    const text = await response.text()
    let parsed: unknown = text
    if (text) {
      try {
        parsed = JSON.parse(text)
      } catch {
        parsed = text
      }
    }

    if (!response.ok) {
      throw new ApiError('request failed', response.status, parsed)
    }
    return (text ? parsed : null) as T
  } catch (error) {
    if (error instanceof ApiError) throw error
    if (error instanceof Error && error.name === 'AbortError') {
      throw new ApiError('timeout')
    }
    throw new ApiError('offline')
  } finally {
    clearTimeout(timeout)
  }
}

// —— 状态归一化（容错核心） ——

const PERCENT_KEYS = ['percent', 'value', 'pct', 'level', 'score', 'ratio'] as const

/** 数字/数字字符串/0–1 小数/包装对象 → 0–100；识别不了返回 undefined，绝不臆造 0。 */
export function normalizePercent(value: unknown): number | undefined {
  let candidate: unknown = value
  if (isRecord(candidate)) {
    const boxed = candidate
    candidate = PERCENT_KEYS
      .map((key) => boxed[key])
      .find((entry) => entry !== undefined && entry !== null)
  }
  if (typeof candidate === 'string') {
    const trimmed = candidate.trim().replace(/%$/, '')
    candidate = trimmed === '' ? Number.NaN : Number(trimmed)
  }
  if (typeof candidate !== 'number' || !Number.isFinite(candidate)) return undefined
  const scaled = candidate >= 0 && candidate <= 1 ? candidate * 100 : candidate
  return Math.round(Math.min(100, Math.max(0, scaled)))
}

export function normalizeLabel(value: unknown): string | undefined {
  if (typeof value === 'string') {
    const trimmed = value.trim()
    return trimmed === '' ? undefined : trimmed.slice(0, 40)
  }
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  if (isRecord(value)) {
    for (const key of ['label', 'name', 'value', 'state'] as const) {
      const nested = normalizeLabel(value[key])
      if (nested) return nested
    }
  }
  return undefined
}

export function normalizeFlag(value: unknown): boolean | undefined {
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') return Number.isFinite(value) ? value !== 0 : undefined
  if (typeof value === 'string') {
    const lowered = value.trim().toLowerCase()
    if (['true', '1', 'on', 'yes', 'y', '是', '开'].includes(lowered)) return true
    if (['false', '0', 'off', 'no', 'n', '否', '关'].includes(lowered)) return false
  }
  if (isRecord(value)) {
    for (const key of ['enabled', 'quiet', 'muted', 'active', 'value'] as const) {
      const nested = normalizeFlag(value[key])
      if (nested !== undefined) return nested
    }
  }
  return undefined
}

/**
 * 提取界面状态。**永不抛错**：任何畸形输入都退化为「字段缺失」。
 * 字段名以后端实现为准（fatigue / in_quiet_hours），同时兼容早期约定名。
 */
export function normalizeState(payload: StatePayload): LingState {
  const wrapped = isRecord(payload) && isRecord(payload.state) ? payload.state : undefined
  const source: RawState = wrapped ?? (isRecord(payload) ? payload : {})

  const state: LingState = {
    energy: normalizePercent(source.energy),
    satiety: normalizePercent(source.satiety),
    tiredness: normalizePercent(source.fatigue) ?? normalizePercent(source.tiredness),
    mood: normalizeLabel(source.mood),
    presence: normalizeLabel(source.presence),
    quiet:
      normalizeFlag(source.in_quiet_hours) ??
      normalizeFlag(source.quiet) ??
      normalizeFlag(source.muted),
    affection: normalizePercent(source.affection),
    hungry: normalizeFlag(source.hungry),
    tired: normalizeFlag(source.tired),
  }

  return Object.fromEntries(
    Object.entries(state).filter(([, value]) => value !== undefined),
  ) as LingState
}

/** 新值覆盖旧值、缺失字段保留旧值，避免刷新时数值闪回空白。 */
export function mergeState(previous: LingState, next: LingState): LingState {
  return { ...previous, ...next }
}

export function hasUsableState(state: LingState): boolean {
  return Object.values(state).some((value) => value !== undefined && value !== null)
}

export function stateFieldCount(state: LingState): number {
  return Object.keys(state).length
}

// —— HTTP 接口 ——

export async function checkHealth(token: string): Promise<void> {
  await request<unknown>('/v1/health', token)
}

export async function fetchState(token: string): Promise<LingState> {
  return normalizeState(await request<StatePayload>('/v1/state', token))
}

export async function sendChat(message: string, clientId: string, token: string): Promise<ChatResponse> {
  return request<ChatResponse>('/v1/chat', token, {
    method: 'POST',
    body: JSON.stringify({ message, client_id: clientId }),
  })
}

let interactionSupported: boolean | undefined

/**
 * 互动上报：POST /v1/state/interact。
 * kind 必须属于后端 InteractionKind。响应带回最新状态，可直接采用。
 * 4xx（端点缺失或契约不符）只失败一次就停用，不打扰用户。
 */
export async function postInteraction(
  token: string,
  kind: InteractionKind,
  clientId: string,
): Promise<LingState | null> {
  if (interactionSupported === false) return null
  try {
    const payload = await request<StatePayload>('/v1/state/interact', token, {
      method: 'POST',
      body: JSON.stringify({ kind, client_id: clientId, count: 1 }),
    })
    interactionSupported = true
    return normalizeState(payload)
  } catch (error) {
    if (error instanceof ApiError && (error.status ?? 0) >= 400 && (error.status ?? 0) < 500) {
      interactionSupported = false
    }
    return null
  }
}

export async function fetchEmotions(token: string, mood?: string, energy?: number): Promise<EmotionsResponse> {
  const params = new URLSearchParams()
  if (mood) params.set('mood', mood)
  if (energy !== undefined) params.set('energy', String(energy))
  const query = params.toString()
  const raw = await request<Partial<EmotionsResponse>>('/v1/emotions' + (query ? '?' + query : ''), token)
  const recommended = isRecord(raw?.recommended) ? (raw.recommended as EmotionRecommendation) : {}
  return {
    items: Array.isArray(raw?.items) ? (raw.items as EmotionItem[]) : [],
    recommended,
    triggers: {
      self_portrait: Array.isArray(raw?.triggers?.self_portrait) ? raw.triggers.self_portrait : [],
      emotion: Array.isArray(raw?.triggers?.emotion) ? raw.triggers.emotion : [],
    },
    kinds: Array.isArray(raw?.kinds) ? raw.kinds : [],
  }
}

export async function renderEmotion(
  token: string,
  body: { trigger?: string; emotion?: string; kind?: 'proactive' | 'reply'; mood?: string; energy?: number },
): Promise<RenderResult> {
  return request<RenderResult>('/v1/emotions/render', token, {
    method: 'POST',
    body: JSON.stringify(body),
    timeoutMs: IMAGE_TIMEOUT_MS,
  })
}

export async function generateImage(
  token: string,
  body: { prompt: string; reason: string; mood?: string; size?: number; client_id?: string },
): Promise<ImageGenerateResponse> {
  return request<ImageGenerateResponse>('/v1/image/generate', token, {
    method: 'POST',
    body: JSON.stringify({ size: 1024, ...body }),
    timeoutMs: IMAGE_TIMEOUT_MS,
  })
}

export async function listArtwork(token: string, limit = 24): Promise<ArtworkItem[]> {
  const raw = await request<{ items?: ArtworkItem[] }>('/v1/artwork?limit=' + limit, token)
  return Array.isArray(raw?.items) ? raw.items : []
}

/**
 * 取回作品图片。
 *
 * 后端 /v1/artwork/{id}/file 需要 X-Ling-Token 头，<img src> 带不了，
 * 所以必须 fetch 成 blob 再转 object URL。调用方负责在不用时 revoke。
 * artwork 缺 id 时返回 null（例如表情没画成、只有文字）。
 */
export async function fetchArtworkBlobUrl(token: string, artworkId: string): Promise<string | null> {
  if (!artworkId) return null
  try {
    const response = await fetch(API_BASE_URL + '/v1/artwork/' + encodeURIComponent(artworkId) + '/file', {
      headers: { 'X-Ling-Token': token },
    })
    if (!response.ok) return null
    const blob = await response.blob()
    return URL.createObjectURL(blob)
  } catch {
    return null
  }
}

/** 双击静音时驱动原生点穿；只在 Tauri 里有效。 */
export async function setClickThrough(through: boolean): Promise<boolean> {
  if (!isTauri()) return false
  try {
    return Boolean(await invoke<boolean>('set_click_through', { through }))
  } catch {
    return false
  }
}
