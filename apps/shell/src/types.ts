/**
 * 灵鲸鲸桌面壳的共享类型。
 *
 * 约定：后端 /v1/state 的字段**全部可选**。M0 只给 energy/mood/presence，
 * M1 之后增加 satiety/fatigue/in_quiet_hours/affection 等。前端必须能容错降级，
 * 不能因为某个字段缺席就报错或白屏。
 */

/** 归一化之后的界面状态：已知字段可选；缺席时为 undefined，界面显示「—」。 */
export interface LingState {
  energy?: number
  satiety?: number
  /** 后端字段名为 fatigue；也接受 tiredness。0 = 不累 / 100 = 极累。 */
  tiredness?: number
  mood?: string
  presence?: string
  /** 后端字段名为 in_quiet_hours；也接受 quiet / muted。 */
  quiet?: boolean
  /** 好感度（0–100），M1 新增。 */
  affection?: number
  /** 后端派生的「饿」「累」布尔，比前端阈值更权威。 */
  hungry?: boolean
  tired?: boolean
}

export type RawState = Record<string, unknown>

export interface ChatResponse {
  reply: string
  state?: unknown
}

export type MessageRole = 'user' | 'assistant' | 'system'

export interface ChatMessage {
  id: string
  role: MessageRole
  content: string
  time: string
  /** 附带的图片（表情包 / 生图结果），用 blob URL 展示。 */
  imageUrl?: string
  imageAlt?: string
}

export type ConnectionStatus = 'connecting' | 'online' | 'offline' | 'stale'

/** 后端 InteractionKind 字面量；与 apps/brain/.../schemas.py 保持一致。 */
export type InteractionKind = 'drag' | 'stroke' | 'feed' | 'poke' | 'wake' | 'play'

/** 气泡：本地互动/状态提醒，不占用聊天记录。 */
export interface BubbleMessage {
  id: string
  text: string
  tone: 'info' | 'success' | 'warn' | 'offline'
  time: string
}

/** 顶层视图。用左侧窄导航切换，所有一级功能一键可达。 */
export type ViewId = 'chat' | 'mood' | 'gallery' | 'state'

/** GET /v1/emotions 返回的单个表情。 */
export interface EmotionItem {
  id: string
  name: string
  emoji: string
  description: string
  moods: string[]
  energy_range: [number, number]
  self_portrait: boolean
}

/** 后端 recommend() 返回的结构：按当前心情/能量推荐的表情与自画像。 */
export interface EmotionRecommendation {
  emotion?: EmotionItem
  self_portrait?: EmotionItem
  mood?: string
  energy?: number
  imagegen?: { available?: boolean; backend?: string; paused?: string | null }
}

export interface EmotionsResponse {
  items: EmotionItem[]
  recommended: EmotionRecommendation
  triggers: { self_portrait: string[]; emotion: string[] }
  kinds: string[]
}

/** POST /v1/emotions/render 返回。 */
export interface RenderResult {
  id?: string
  artwork_id?: string
  emotion: EmotionItem
  kind: string
  action: string
  path: string | null
  prompt: string
  reason: string
  mood: string
  energy: number
  backend: string
  degraded: boolean
  error: string | null
  text: string
  trigger?: Record<string, unknown>
}

export interface ArtworkItem {
  id: string
  path: string
  prompt: string
  reason: string
  mood: string | null
  created_at: string
}

export interface ImageGenerateResponse {
  artwork: ArtworkItem
  queue: { state: string; task_id: string; history: string[] }
  budget: Record<string, unknown>
  seed: number
}

/**
 * 面向用户的错误：只说人话，不暴露 HTTP 状态码。
 * kind 供程序分支，message 直接展示给用户。
 */
export type FriendlyErrorKind =
  | 'offline'
  | 'timeout'
  | 'auth'
  | 'busy'
  | 'budget'
  | 'not-found'
  | 'server'
  | 'bad-request'
  | 'unknown'

export interface FriendlyError {
  kind: FriendlyErrorKind
  message: string
  /** 可以给用户看的下一步建议；没有就不显示。 */
  hint?: string
}
