/**
 * 类型定义：严格对照 docs/api-v4.md（V4 契约）。
 * 契约里没有的字段一律不加；可选性按契约原文标注。
 */

// ───────────────────────────── 契约 1：聊天历史 ─────────────────────────────

export type ChatRole = 'user' | 'assistant'

/** GET /v1/chat/history 的 items 元素。role 只有 user/assistant。 */
export interface HistoryMessage {
  id: string
  ts: string
  role: ChatRole
  content: string
  client_id: string
  /** 仅 assistant 可能带。 */
  model?: string
}

export interface HistoryResponse {
  items: HistoryMessage[]
  has_more: boolean
}

/**
 * 前端把「一条 user + 紧随其后的 assistant」分成一组（契约明确规定后端不分组）。
 * 因此这里的分组结果可能是不完整的（例如最后一条 user 还没等到回复）。
 */
export interface ChatRound {
  id: string
  index: number
  user?: HistoryMessage
  assistant?: HistoryMessage
  /** 该轮在列表里的时间（取 user 的 ts，没有则取 assistant）。 */
  ts: string
}

// ─────────────────────────── 契约 2：模型提供商 ───────────────────────────

export type ProviderKind = 'ollama' | 'openai_compatible'
export type ProviderStatus = 'ok' | 'down' | 'unknown' | 'unchecked'
export type Capability = 'chat' | 'extract' | 'image' | 'tts'
export type RoutingMode = 'single' | 'concurrent'

export interface Provider {
  id: string
  name: string
  kind: ProviderKind
  base_url: string
  model: string
  /** 契约：api_key 只写不读，GET 只返回 has_api_key。 */
  has_api_key: boolean
  enabled: boolean
  capabilities: string[]
  status: ProviderStatus
  is_builtin: boolean
  last_checked_at: string | null
}

export interface Routing {
  mode: RoutingMode
  active_provider_id: string | null
  enabled_ids: string[]
  /** 键为任务类型：chat | extract | image | tts。 */
  assignments: Record<string, string>
}

export interface ProvidersResponse {
  items: Provider[]
  routing: Routing
  limits: { max_enabled: number }
}

/** POST /v1/providers 请求体。 */
export interface ProviderCreate {
  name: string
  kind: ProviderKind
  base_url: string
  model: string
  api_key?: string
  capabilities?: string[]
}

/**
 * PUT /v1/providers/{id}：字段全部可选。
 * api_key 语义特殊：undefined = 不传（不修改）；"" = 不修改；null = 清除。
 */
export interface ProviderUpdate {
  name?: string
  kind?: ProviderKind
  base_url?: string
  model?: string
  api_key?: string | null
  capabilities?: string[]
}

export interface ProviderTestResult {
  ok: boolean
  latency_ms: number
  models: string[]
  error: string | null
}

export interface RoutingUpdate {
  mode: RoutingMode
  active_provider_id?: string | null
  enabled_ids?: string[]
}

// ───────────────────────────── 既有接口（沿用） ─────────────────────────────

export interface ChatResponse {
  reply: string
  state?: unknown
}

export type InteractionKind = 'drag' | 'stroke' | 'feed' | 'poke' | 'wake' | 'play'

/**
 * 状态字段全部可选（后端 M0→M1 逐步增加字段）。
 * 字段名以后端实现为准（fatigue / in_quiet_hours），旧名兼容。
 */
export interface LingState {
  energy?: number
  satiety?: number
  tiredness?: number
  mood?: string
  presence?: string
  quiet?: boolean
  affection?: number
  hungry?: boolean
  tired?: boolean
}

export interface ArtworkItem {
  id: string
  path: string
  prompt: string
  reason: string
  mood: string | null
  created_at: string
}

export interface EmotionItem {
  id: string
  name: string
  emoji: string
  description: string
  moods: string[]
  energy_range: [number, number]
  self_portrait: boolean
}

// ───────────────────── 契约：部署配置（task-22） ─────────────────────

/**
 * GET /v1/deployment 返回 {mode, server_url, token}（token 可能为空串）；
 * PUT 请求体同结构，校验失败 422。
 */
export interface DeploymentDoc {
  mode: 'local' | 'terminal' | 'server'
  server_url: string
  token: string
}

// ───────────────────────────── 前端内部类型 ─────────────────────────────

/** 五标签导航（docs/ui-v4-layout.md）。 */
export type ViewId = 'chat' | 'interact' | 'gallery' | 'tools' | 'settings'

export type ConnectionStatus = 'connecting' | 'online' | 'offline'

export type AppErrorKind =
  | 'offline'
  | 'timeout'
  | 'auth'
  | 'not-found'
  | 'validation'
  | 'server'
  | 'unknown'

/** 面向用户的错误：message 直接展示，绝不暴露 HTTP 状态码。 */
export interface AppError {
  kind: AppErrorKind
  message: string
  hint?: string
  /** 422 时后端可能给 {"error","problems"}，这里保留可读的问题列表。 */
  problems?: string[]
}

export interface Toast {
  id: string
  tone: 'info' | 'success' | 'error'
  message: string
}
