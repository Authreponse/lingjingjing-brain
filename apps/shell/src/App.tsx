import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type {
  AppError, ArtworkItem, ChatRound, ConnectionStatus, HistoryMessage,
  InteractionKind, LingState, Toast, ViewId,
} from './types'
import {
  checkHealth, fetchArtworkBlob, fetchDeployment, fetchHistory, fetchState, hasUsableState,
  listArtwork, normalizeState, postInteraction, putDeployment, sendChat, toAppError,
} from './lib/api'
import { groupRounds } from './lib/chat'
import {
  isOnboarded, loadDeployment, loadWhaleName, markOnboarded,
  resolveConnection, saveConnection, saveDeployment, saveWhaleName,
  type Deployment,
  type DeploymentSync,
} from './lib/storage'
import { Splash } from './components/Splash'
import { Onboarding } from './components/Onboarding'
import { WhaleAvatar } from './components/WhaleAvatar'
import { StatusRail } from './components/StatusRail'
import {
  IconChat, IconGallery, IconSettings, IconState, IconToolbox,
} from './icons'
import { ChatPage } from './pages/ChatPage'
import { GalleryPage } from './pages/GalleryPage'
import { StatePage } from './pages/StatePage'
import { ToolsPage } from './pages/ToolsPage'
import { SettingsPage } from './pages/SettingsPage'

const CLIENT_KEY = 'lingjingjing.shell.client-id'
const MUTED_KEY = 'lingjingjing.shell.muted'
const REFRESH_MS = 20000
const HISTORY_LIMIT = 200

const NAV: { id: ViewId; label: string; icon: typeof IconChat }[] = [
  { id: 'chat', label: '聊天', icon: IconChat },
  { id: 'interact', label: '互动', icon: IconState },
  { id: 'gallery', label: '历史画作', icon: IconGallery },
  { id: 'tools', label: '小鲸鱼的工具', icon: IconToolbox },
  { id: 'settings', label: '设置', icon: IconSettings },
]

function newId(): string {
  return globalThis.crypto?.randomUUID?.() ?? Date.now().toString(36) + Math.random().toString(36).slice(2)
}

function readClientId(): string {
  try {
    const existing = window.localStorage.getItem(CLIENT_KEY)
    if (existing) return existing
    const created = newId()
    window.localStorage.setItem(CLIENT_KEY, created)
    return created
  } catch {
    return newId()
  }
}

export default function App() {
  const [splashDone, setSplashDone] = useState(false)
  // ?seen=1 仅用于无头浏览器截图验收（localStorage 每次全新，引导层会挡住主界面）；生产行为不受影响。
  const [onboarded, setOnboarded] = useState(() => isOnboarded() || new URLSearchParams(window.location.search).get('seen') === '1')
  const [deployment, setDeployment] = useState<Deployment | null>(() => loadDeployment())
  // 部署配置与后端的同步状态：synced = 已写入 /v1/deployment；cached = 端点不可用，仅存本地（需在 UI 标注降级）
  const [deploymentSync, setDeploymentSync] = useState<DeploymentSync>('unknown')
  const [cfg, setCfg] = useState(() => resolveConnection())
  const [whaleName, setWhaleName] = useState(() => loadWhaleName())
  const [muted, setMuted] = useState(() => {
    try { return window.localStorage.getItem(MUTED_KEY) === '1' } catch { return false }
  })
  const [clientId] = useState(readClientId)
  const [view, setView] = useState<ViewId>(() => {
    const v = new URLSearchParams(window.location.search).get('view')
    const allowed: ViewId[] = ['chat', 'interact', 'gallery', 'tools', 'settings']
    return v && (allowed as string[]).includes(v) ? (v as ViewId) : 'chat'
  })

  const [connection, setConnection] = useState<ConnectionStatus>('connecting')
  const [connError, setConnError] = useState<AppError | null>(null)
  const [state, setState] = useState<LingState>({})

  const [messages, setMessages] = useState<HistoryMessage[]>([])
  const [historyLoading, setHistoryLoading] = useState(true)
  const [historyError, setHistoryError] = useState<AppError | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [liveRounds, setLiveRounds] = useState<ChatRound[]>([])

  const [artworks, setArtworks] = useState<ArtworkItem[]>([])
  const [artUrls, setArtUrls] = useState<Record<string, string>>({})
  const [galleryLoading, setGalleryLoading] = useState(false)
  const [galleryError, setGalleryError] = useState<AppError | null>(null)
  const objectUrls = useRef<string[]>([])

  const [interacting, setInteracting] = useState<InteractionKind | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [toasts, setToasts] = useState<Toast[]>([])

  const pushToast = useCallback((tone: Toast['tone'], message: string) => {
    const id = newId()
    setToasts((cur) => [...cur.slice(-2), { id, tone, message }])
    window.setTimeout(() => setToasts((cur) => cur.filter((t) => t.id !== id)), 3200)
  }, [])

  useEffect(() => () => { objectUrls.current.forEach((u) => URL.revokeObjectURL(u)) }, [])

  const toggleMute = useCallback(() => {
    setMuted((cur) => {
      const next = !cur
      try { window.localStorage.setItem(MUTED_KEY, next ? '1' : '0') } catch { /* 忽略 */ }
      return next
    })
  }, [])

  // ── 连接与状态 ──
  const refreshState = useCallback(async () => {
    setRefreshing(true)
    try {
      await checkHealth(cfg)
      const next = await fetchState(cfg)
      if (hasUsableState(next)) setState((cur) => ({ ...cur, ...next }))
      setConnection('online')
      setConnError(null)
    } catch (err) {
      setConnection('offline')
      setConnError(toAppError(err))
    } finally {
      setRefreshing(false)
    }
  }, [cfg])

  useEffect(() => {
    void refreshState()
    const timer = window.setInterval(() => { void refreshState() }, REFRESH_MS)
    return () => window.clearInterval(timer)
  }, [refreshState])

  // ── 历史 ──
  const loadHistory = useCallback(async () => {
    setHistoryLoading(true)
    setHistoryError(null)
    try {
      const data = await fetchHistory(cfg, HISTORY_LIMIT)
      setMessages(data.items)
      setHasMore(data.has_more)
    } catch (err) {
      setHistoryError(toAppError(err))
    } finally {
      setHistoryLoading(false)
    }
  }, [cfg])

  useEffect(() => { void loadHistory() }, [loadHistory])

  const loadMore = useCallback(async () => {
    const oldest = messages[0]
    if (!oldest) return
    try {
      const data = await fetchHistory(cfg, HISTORY_LIMIT, oldest.ts)
      setMessages((cur) => [...data.items, ...cur])
      setHasMore(data.has_more)
    } catch (err) {
      pushToast('error', toAppError(err).message)
    }
  }, [cfg, messages, pushToast])

  const historyRounds = useMemo(() => groupRounds(messages), [messages])

  // ── 发送 ──
  const handleSend = useCallback(async () => {
    const text = draft.trim()
    if (!text || sending) return
    const roundId = newId()
    const ts = new Date().toISOString()
    const userMsg: HistoryMessage = { id: roundId + '-u', ts, role: 'user', content: text, client_id: clientId }

    setDraft('')
    setSending(true)
    setLiveRounds((cur) => [...cur, { id: roundId, index: -1, user: userMsg, ts }])

    try {
      const response = await sendChat(cfg, text, clientId)
      const assistantMsg: HistoryMessage = {
        id: roundId + '-a', ts: new Date().toISOString(), role: 'assistant',
        content: typeof response.reply === 'string' ? response.reply : '（没有收到回复）',
        client_id: clientId,
      }
      setLiveRounds((cur) => cur.map((r) => (r.id === roundId ? { ...r, assistant: assistantMsg } : r)))
      setConnection('online')
      setConnError(null)
      if (response.state) {
        const patch = normalizeState(response.state)
        if (hasUsableState(patch)) setState((cur) => ({ ...cur, ...patch }))
      }
    } catch (err) {
      const friendly = toAppError(err)
      setConnError(friendly)
      setConnection('offline')
      setLiveRounds((cur) =>
        cur.map((r) =>
          r.id === roundId
            ? {
                ...r,
                assistant: {
                  id: roundId + '-a', ts: new Date().toISOString(), role: 'assistant' as const,
                  content: friendly.message + (friendly.hint ? '（' + friendly.hint + '）' : ''),
                  client_id: clientId,
                },
              }
            : r,
        ),
      )
    } finally {
      setSending(false)
    }
  }, [cfg, clientId, draft, sending])

  // ── 画廊 ──
  const loadGallery = useCallback(async () => {
    setGalleryLoading(true)
    setGalleryError(null)
    try {
      const items = await listArtwork(cfg)
      setArtworks(items)
      const entries = await Promise.all(
        items.map(async (item) => {
          const blob = await fetchArtworkBlob(cfg, item.id)
          if (!blob) return null
          const url = URL.createObjectURL(blob)
          objectUrls.current.push(url)
          return [item.id, url] as const
        }),
      )
      setArtUrls((cur) => {
        const next = { ...cur }
        for (const e of entries) if (e) next[e[0]] = e[1]
        return next
      })
    } catch (err) {
      setGalleryError(toAppError(err))
    } finally {
      setGalleryLoading(false)
    }
  }, [cfg])

  useEffect(() => {
    if (view === 'gallery' && artworks.length === 0 && !galleryLoading) void loadGallery()
  }, [view, artworks.length, galleryLoading, loadGallery])

  const interact = useCallback(async (kind: InteractionKind) => {
    if (interacting) return
    setInteracting(kind)
    try {
      const next = await postInteraction(cfg, kind, clientId)
      if (next && hasUsableState(next)) setState((cur) => ({ ...cur, ...next }))
      else pushToast('info', '这个互动暂时还不可用')
    } finally {
      setInteracting(null)
    }
  }, [cfg, clientId, interacting, pushToast])

  // ── 部署 / 名字 ──
  // 连上后端后拉一次服务端保存的部署配置；端点不可用（404/网络错误）时保持本地缓存并标记降级。
  useEffect(() => {
    if (connection !== 'online') return
    let cancelled = false
    void (async () => {
      try {
        const doc = await fetchDeployment(cfg)
        if (cancelled) return
        setDeploymentSync('synced')
        // 服务端有配置才采用；否则保留本地（可能用户刚在离线时选过）
        if (doc.mode) {
          saveDeployment(doc)
          setDeployment(doc)
        }
      } catch {
        if (!cancelled) setDeploymentSync('cached')
      }
    })()
    return () => { cancelled = true }
  }, [cfg, connection])

  /** 保存部署配置：先写后端，失败则退回本地缓存并明确标记降级。 */
  const applyDeployment = async (dep: Deployment) => {
    saveDeployment(dep)
    setDeployment(dep)
    setCfg(resolveConnection())
    markOnboarded()
    setOnboarded(true)
    try {
      await putDeployment({ baseUrl: resolveConnection().baseUrl, token: resolveConnection().token }, dep)
      setDeploymentSync('synced')
    } catch {
      setDeploymentSync('cached')
      pushToast('info', '本地已保存，但服务器暂时没记住（端点不可用）')
    }
  }

  const applyName = (name: string) => {
    saveWhaleName(name)
    setWhaleName(name)
  }

  const applyConnection = (baseUrl: string, token: string) => {
    saveConnection(baseUrl, token)
    setCfg({ baseUrl: baseUrl.replace(/\/$/, ''), token })
  }

  const online = connection === 'online'
  const brandTone = online ? 'online' : connection === 'offline' ? 'offline' : 'connecting'

  return (
    <div className="app">
      <div className="aurora" aria-hidden="true"><span /><span /><span /></div>
      <div className="noise" aria-hidden="true" />

      <div className="window">
        {/* 顶栏：左名字，右圆形头像（点击切静音） */}
        <header className="titlebar">
          <div className="brand" data-tauri-drag-region>
            <span className={'brand-dot brand-dot--' + brandTone} aria-hidden="true" />
            <span className="brand-name">{whaleName}</span>
            <span className="brand-status">
              {online ? (muted ? '静音中' : '在线') : connection === 'offline' ? '离线' : '连接中'}
            </span>
          </div>
          <WhaleAvatar
            size={34}
            online={online}
            muted={muted}
            label={whaleName}
            onClick={toggleMute}
          />
        </header>

        {/* 导航：一行五个等宽胶囊 */}
        <nav className="tabs" role="tablist" aria-label="主导航">
          {NAV.map((item) => {
            const Icon = item.icon
            return (
              <button
                key={item.id}
                type="button"
                role="tab"
                aria-selected={view === item.id}
                className={'tab' + (view === item.id ? ' is-current' : '')}
                onClick={() => setView(item.id)}
              >
                <Icon size={16} />
                <span>{item.label}</span>
              </button>
            )
          })}
        </nav>

        <div className="body">
          {/* 左侧 40px 细栏：状态徽章 */}
          <StatusRail
            state={state}
            online={online}
            muted={muted}
            onGoInteract={() => setView('interact')}
          />

          <main className="main">
            {connection === 'offline' && connError && (
              <div style={{ padding: '12px 20px 0' }}>
                <div className="alert alert--error" role="alert">
                  <div>
                    <strong>{connError.message}</strong>
                    <span>{connError.hint}</span>
                  </div>
                </div>
              </div>
            )}

            {view === 'chat' && (
              <ChatPage
                rounds={historyRounds}
                historyLoading={historyLoading}
                historyError={historyError}
                hasMore={hasMore}
                onLoadMore={() => void loadMore()}
                sending={sending}
                draft={draft}
                onDraft={setDraft}
                onSend={() => void handleSend()}
                connection={connection}
                liveRounds={liveRounds}
              />
            )}
            {view === 'interact' && (
              <StatePage
                state={state}
                connection={connection}
                error={connError}
                onRefresh={() => void refreshState()}
                refreshing={refreshing}
                interacting={interacting}
                onInteract={(kind) => void interact(kind)}
              />
            )}
            {view === 'gallery' && (
              <GalleryPage
                items={artworks}
                urls={artUrls}
                loading={galleryLoading}
                error={galleryError}
                onRefresh={() => void loadGallery()}
              />
            )}
            {view === 'tools' && (
              <ToolsPage
                onGoGallery={() => setView('gallery')}
                onGoModels={() => setView('settings')}
                onGoInteract={() => setView('interact')}
                error={connError}
              />
            )}
            {view === 'settings' && (
              <SettingsPage
                cfg={cfg}
                whaleName={whaleName}
                deployment={deployment}
                deploymentSync={deploymentSync}
                onSaveName={applyName}
                onSaveDeployment={(dep) => void applyDeployment(dep)}
                onSaveConnection={applyConnection}
                onToast={pushToast}
              />
            )}
          </main>
        </div>

        <div className="peek-toast" aria-live="polite">
          {toasts.map((t) => (
            <div key={t.id} className={'toast toast--' + t.tone}>{t.message}</div>
          ))}
        </div>
      </div>

      {!splashDone && (
        <Splash ready={connection !== 'connecting'} onDone={() => setSplashDone(true)} />
      )}

      {splashDone && !onboarded && (
        <Onboarding onDone={(dep) => void applyDeployment(dep)} />
      )}
    </div>
  )
}
