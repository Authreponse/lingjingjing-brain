import { useCallback, useEffect, useRef, useState } from 'react'
import { isTauri } from '@tauri-apps/api/core'
import { getCurrentWindow } from '@tauri-apps/api/window'
import {
  checkHealth,
  fetchArtworkBlobUrl,
  fetchEmotions,
  fetchState,
  generateImage,
  hasUsableState,
  listArtwork,
  mergeState,
  normalizeState,
  postInteraction,
  renderEmotion,
  sendChat,
  setClickThrough,
  toFriendlyError,
} from './api'
import { BubbleLayer } from './components/StateBubble'
import { ChatPanel } from './components/ChatPanel'
import { GalleryPanel } from './components/GalleryPanel'
import { MoodPanel } from './components/MoodPanel'
import { SettingsDialog } from './components/SettingsDialog'
import { StatePanel } from './components/StatePanel'
import { Whale } from './components/Whale'
import { describeState, statusTone } from './status'
import { useTrayMenu } from './useTrayMenu'
import type {
  ArtworkItem,
  BubbleMessage,
  ChatMessage,
  ConnectionStatus,
  EmotionItem,
  EmotionRecommendation,
  FriendlyError,
  InteractionKind,
  LingState,
  RenderResult,
  ViewId,
} from './types'

const TOKEN_KEY = 'lingjingjing.shell.token'
const CLIENT_KEY = 'lingjingjing.shell.client-id'
const MUTED_KEY = 'lingjingjing.shell.muted'
const REFRESH_MS = 15_000
const BUBBLE_MS = 3_600

const timeFmt = new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
const now = () => timeFmt.format(new Date())
const newId = () =>
  globalThis.crypto?.randomUUID?.() ?? Date.now().toString(36) + Math.random().toString(36).slice(2)

function readStore(key: string, fallback: string): string {
  try {
    return window.localStorage.getItem(key) || fallback
  } catch {
    return fallback
  }
}
function writeStore(key: string, value: string) {
  try {
    window.localStorage.setItem(key, value)
  } catch {
    // 隐私模式下不可写，忽略。
  }
}

const NAV: { id: ViewId; icon: string; label: string }[] = [
  { id: 'chat', icon: '💬', label: '聊天' },
  { id: 'mood', icon: '😊', label: '心情' },
  { id: 'gallery', icon: '🖼', label: '画廊' },
  { id: 'state', icon: '🐳', label: '状态' },
]

export default function App() {
  const desktop = isTauri()
  const [token, setToken] = useState(() => readStore(TOKEN_KEY, 'lingjingjing-local-dev'))
  const [clientId] = useState(() => {
    const existing = readStore(CLIENT_KEY, '')
    if (existing) return existing
    const created = newId()
    writeStore(CLIENT_KEY, created)
    return created
  })
  const [muted, setMuted] = useState(() => readStore(MUTED_KEY, '0') === '1')

  // 仅用于开发期截图/调试：?view=mood 可直接打开某个面板。生产行为不受影响。
  const [view, setView] = useState<ViewId>(() => {
    try {
      const requested = new URLSearchParams(window.location.search).get('view')
      if (requested === 'mood' || requested === 'gallery' || requested === 'state' || requested === 'chat') {
        return requested
      }
    } catch {
      // 忽略
    }
    return 'chat'
  })
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [pinned, setPinned] = useState(true)
  const [notice, setNotice] = useState('')

  const [connection, setConnection] = useState<ConnectionStatus>('connecting')
  const [state, setState] = useState<LingState>({})
  const [lastKnown, setLastKnown] = useState<LingState | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [connError, setConnError] = useState<FriendlyError | null>(null)

  const [messages, setMessages] = useState<ChatMessage[]>([
    { id: 'hello', role: 'system', content: '我在这儿。想聊什么都可以。', time: now() },
  ])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)

  const [emotions, setEmotions] = useState<EmotionItem[]>([])
  const [recommended, setRecommended] = useState<EmotionRecommendation>({})
  const [emotionsLoading, setEmotionsLoading] = useState(false)
  const [rendering, setRendering] = useState<string | null>(null)
  const [renderResult, setRenderResult] = useState<RenderResult | null>(null)
  const [renderUrl, setRenderUrl] = useState<string | null>(null)
  const [moodError, setMoodError] = useState<FriendlyError | null>(null)

  const [artworks, setArtworks] = useState<ArtworkItem[]>([])
  const [artUrls, setArtUrls] = useState<Record<string, string>>({})
  const [galleryLoading, setGalleryLoading] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [galleryError, setGalleryError] = useState<FriendlyError | null>(null)

  const [interacting, setInteracting] = useState<InteractionKind | null>(null)
  const [bubbles, setBubbles] = useState<BubbleMessage[]>([])
  const objectUrls = useRef<string[]>([])

  const hasState = lastKnown !== null || hasUsableState(state)

  // —— 气泡：自动消失，最多保留两条，避免刷屏 ——
  const say = useCallback((text: string, tone: BubbleMessage['tone'] = 'info') => {
    const id = newId()
    setBubbles((current) => [...current.slice(-1), { id, text, tone, time: now() }])
    window.setTimeout(() => {
      setBubbles((current) => current.filter((b) => b.id !== id))
    }, BUBBLE_MS)
  }, [])

  // 组件卸载时释放所有 blob URL，避免内存泄漏
  useEffect(() => {
    const urls = objectUrls.current
    return () => {
      urls.forEach((url) => URL.revokeObjectURL(url))
    }
  }, [])

  const trackUrl = useCallback((url: string | null) => {
    if (url) objectUrls.current.push(url)
    return url
  }, [])

  const applyState = useCallback((next: LingState) => {
    if (!hasUsableState(next)) return
    setState((current) => mergeState(current, next))
    setLastKnown((current) => (current ? mergeState(current, next) : next))
  }, [])

  // —— 连接与状态轮询 ——
  const refresh = useCallback(async () => {
    setRefreshing(true)
    try {
      await checkHealth(token)
      const next = await fetchState(token)
      applyState(next)
      setConnection('online')
      setConnError(null)
    } catch (error) {
      setConnection('offline')
      setConnError(toFriendlyError(error))
    } finally {
      setRefreshing(false)
    }
  }, [applyState, token])

  useEffect(() => {
    setConnection('connecting')
    void refresh()
    const timer = window.setInterval(() => void refresh(), REFRESH_MS)
    return () => window.clearInterval(timer)
  }, [refresh])

  // —— 表情列表：进入心情页时才拉，减少无谓请求 ——
  useEffect(() => {
    if (view !== 'mood' || emotions.length > 0) return
    let alive = true
    setEmotionsLoading(true)
    void fetchEmotions(token, state.mood, state.energy)
      .then((data) => {
        if (alive) {
          setEmotions(data.items)
          setRecommended(data.recommended)
        }
      })
      .catch((error) => {
        if (alive) setMoodError(toFriendlyError(error))
      })
      .finally(() => {
        if (alive) setEmotionsLoading(false)
      })
    return () => {
      alive = false
    }
  }, [view, token, emotions.length, state.mood, state.energy])

  // —— 画廊：进入时才拉，并逐张换取带鉴权的图片 ——
  const loadGallery = useCallback(async () => {
    setGalleryLoading(true)
    setGalleryError(null)
    try {
      const items = await listArtwork(token)
      setArtworks(items)
      const entries = await Promise.all(
        items.map(async (item) => [item.id, trackUrl(await fetchArtworkBlobUrl(token, item.id))] as const),
      )
      setArtUrls((current) => {
        const next = { ...current }
        for (const [id, url] of entries) if (url) next[id] = url
        return next
      })
    } catch (error) {
      setGalleryError(toFriendlyError(error))
    } finally {
      setGalleryLoading(false)
    }
  }, [token, trackUrl])

  useEffect(() => {
    if (view === 'gallery' && artworks.length === 0 && !galleryLoading) void loadGallery()
  }, [view, artworks.length, galleryLoading, loadGallery])

  // —— 聊天 ——
  const handleSend = useCallback(async () => {
    const text = input.trim()
    if (!text || sending) return
    setMessages((m) => [...m, { id: newId(), role: 'user', content: text, time: now() }])
    setInput('')
    setSending(true)
    try {
      const response = await sendChat(text, clientId, token)
      if (!response || typeof response.reply !== 'string') {
        throw new Error('bad reply')
      }
      setMessages((m) => [...m, { id: newId(), role: 'assistant', content: response.reply, time: now() }])
      if (response.state !== undefined && response.state !== null) {
        applyState(normalizeState(response.state as Record<string, unknown>))
      }
      setConnection((c) => (c === 'offline' ? 'online' : c))
      setConnError(null)
    } catch (error) {
      const friendly = toFriendlyError(error)
      setConnection('offline')
      setConnError(friendly)
      setMessages((m) => [
        ...m,
        { id: newId(), role: 'system', content: friendly.message + (friendly.hint ? '：' + friendly.hint : ''), time: now() },
      ])
    } finally {
      setSending(false)
    }
  }, [applyState, clientId, input, sending, token])

  // —— 互动 ——
  const interact = useCallback(
    async (kind: InteractionKind) => {
      if (interacting) return
      setInteracting(kind)
      try {
        const next = await postInteraction(token, kind, clientId)
        if (next) applyState(next)
        say(kind === 'feed' ? '吃饱啦，谢谢你' : kind === 'stroke' ? '呼噜呼噜…' : '好耶', 'success')
      } finally {
        setInteracting(null)
      }
    },
    [applyState, clientId, interacting, say, token],
  )

  // —— 表情 ——
  const renderOne = useCallback(
    async (body: { trigger?: string; emotion?: string }, key: string) => {
      if (rendering) return
      setRendering(key)
      setMoodError(null)
      setRenderResult(null)
      setRenderUrl(null)
      try {
        const result = await renderEmotion(token, { ...body, mood: state.mood, energy: state.energy })
        setRenderResult(result)
        const id = result.artwork_id ?? result.id
        if (id) setRenderUrl(trackUrl(await fetchArtworkBlobUrl(token, id)))
      } catch (error) {
        setMoodError(toFriendlyError(error))
      } finally {
        setRendering(null)
      }
    },
    [rendering, state.energy, state.mood, token, trackUrl],
  )

  // —— 生图 ——
  const handleGenerate = useCallback(
    async (prompt: string, reason: string) => {
      setGenerating(true)
      setGalleryError(null)
      try {
        const result = await generateImage(token, { prompt, reason, mood: state.mood, client_id: clientId })
        say('画好啦，去画廊看看', 'success')
        const item = result.artwork
        if (item) {
          setArtworks((current) => [item, ...current.filter((a) => a.id !== item.id)])
          const url = trackUrl(await fetchArtworkBlobUrl(token, item.id))
          if (url) setArtUrls((current) => ({ ...current, [item.id]: url }))
        }
      } catch (error) {
        setGalleryError(toFriendlyError(error))
      } finally {
        setGenerating(false)
      }
    },
    [clientId, say, state.mood, token, trackUrl],
  )

  // —— 静音 ——
  const toggleMute = useCallback(async () => {
    const next = !muted
    setMuted(next)
    writeStore(MUTED_KEY, next ? '1' : '0')
    if (desktop) await setClickThrough(next)
    say(next ? '好，我安静陪你' : '我回来啦', next ? 'info' : 'success')
  }, [desktop, muted, say])

  // —— 托盘 ——
  useTrayMenu({
    show: () => {
      if (desktop) void getCurrentWindow().show().catch(() => setNotice('显示窗口失败，请检查权限'))
    },
    hide: () => {
      if (desktop) void getCurrentWindow().hide().catch(() => setNotice('隐藏窗口失败，请检查权限'))
    },
    toggleMute: () => void toggleMute(),
    refresh: () => void refresh(),
    resetWindow: () => {
      if (!desktop) return
      void (async () => {
        try {
          const win = getCurrentWindow()
          await win.unminimize()
          await win.show()
          await win.center()
        } catch {
          setNotice('重置窗口位置失败，请检查权限')
        }
      })()
    },
    openSettings: () => setSettingsOpen(true),
  })

  useEffect(() => {
    if (!desktop) return
    let alive = true
    void getCurrentWindow()
      .isAlwaysOnTop()
      .then((value) => {
        if (alive) setPinned(value)
      })
      .catch(() => undefined)
    return () => {
      alive = false
    }
  }, [desktop])

  const togglePinned = async () => {
    const next = !pinned
    try {
      await getCurrentWindow().setAlwaysOnTop(next)
      setPinned(next)
    } catch {
      setNotice('置顶切换失败，请检查窗口权限')
    }
  }

  // —— 渲染 ——
  const display = describeState({
    state: connection === 'offline' && lastKnown ? lastKnown : state,
    connection,
    hasState,
  })
  const tone = statusTone(connection, state.quiet === true || muted)
  const offline = connection === 'offline'

  return (
    <div className="app">
      <div className="window">
        <header className="titlebar">
          <div className="titlebar-brand" data-tauri-drag-region>
            <span className={'live-dot live-dot--' + tone} aria-hidden="true" />
            <span className="titlebar-title">灵鲸鲸</span>
            <span className="titlebar-sub">
              {muted ? '静音中' : display.headline}
              {display.detail ? ' · ' + display.detail : ''}
            </span>
          </div>
          <button
            type="button"
            className={muted ? 'icon-btn is-active' : 'icon-btn'}
            onClick={() => void toggleMute()}
            aria-pressed={muted}
            title={muted ? '恢复声音' : '静音'}
            aria-label={muted ? '恢复声音' : '静音'}
          >
            {muted ? '🔇' : '🔊'}
          </button>
          {desktop && (
            <button
              type="button"
              className={pinned ? 'icon-btn is-active' : 'icon-btn'}
              onClick={() => void togglePinned()}
              aria-pressed={pinned}
              title={pinned ? '取消置顶' : '始终置顶'}
              aria-label={pinned ? '取消置顶' : '始终置顶'}
            >
              📌
            </button>
          )}
          <button
            type="button"
            className="icon-btn"
            onClick={() => setSettingsOpen(true)}
            aria-label="设置"
            title="设置"
          >
            ⚙
          </button>
          {desktop && (
            <>
              <button type="button" className="icon-btn" onClick={() => getCurrentWindow().minimize()} aria-label="最小化">—</button>
              <button type="button" className="icon-btn icon-btn--danger" onClick={() => getCurrentWindow().close()} aria-label="关闭">×</button>
            </>
          )}
        </header>

        {notice && (
          <div className="alert alert--warn" role="status" style={{ margin: '8px 16px 0' }}>
            <div><span>{notice}</span></div>
          </div>
        )}

        <div className="body">
          <nav className="rail" aria-label="主导航">
            <div className="rail-whale">
              <Whale
                energy={state.energy}
                satiety={state.satiety}
                tiredness={state.tiredness}
                mood={state.mood}
                presence={state.presence}
                quiet={state.quiet === true || muted}
                offline={offline}
                size={64}
              />
            </div>
            {NAV.map((item) => (
              <button
                key={item.id}
                type="button"
                className={view === item.id ? 'rail-btn is-current' : 'rail-btn'}
                onClick={() => setView(item.id)}
                aria-current={view === item.id ? 'page' : undefined}
              >
                <span className="rail-icon" aria-hidden="true">{item.icon}</span>
                <span>{item.label}</span>
              </button>
            ))}
            <div className="rail-spacer" />
          </nav>

          <main className="main">
            {offline && (
              <div style={{ padding: '14px 26px 0' }}>
                <div className="alert alert--error" role="alert">
                  <div>
                    <strong>{connError?.message ?? '联系不上大脑服务'}</strong>
                    <span>{connError?.hint ?? '确认服务开着后，我可以自动重连'}</span>
                  </div>
                </div>
              </div>
            )}

            {view === 'chat' && (
              <ChatPanel
                messages={messages}
                input={input}
                onInputChange={setInput}
                onSend={() => void handleSend()}
                sending={sending}
                disabled={offline}
                offline={offline}
              />
            )}
            {view === 'mood' && (
              <MoodPanel
                emotions={emotions}
                recommended={recommended}
                loading={emotionsLoading}
                rendering={rendering}
                result={renderResult}
                resultUrl={renderUrl}
                error={moodError}
                onRenderSelf={() => void renderOne({ trigger: '画个你自己' }, 'self')}
                onRenderEmotion={(id) => void renderOne({ emotion: id }, id)}
                onArtwork={() => {
                  setView('gallery')
                  void loadGallery()
                }}
              />
            )}
            {view === 'gallery' && (
              <GalleryPanel
                artworks={artworks}
                urls={artUrls}
                loading={galleryLoading}
                generating={generating}
                error={galleryError}
                onGenerate={(prompt, reason) => void handleGenerate(prompt, reason)}
                onRefresh={() => void loadGallery()}
              />
            )}
            {view === 'state' && (
              <StatePanel
                state={connection === 'offline' && lastKnown ? lastKnown : state}
                connection={connection}
                hasState={hasState}
                interacting={interacting}
                onInteract={(kind) => void interact(kind)}
                onRefresh={() => void refresh()}
                refreshing={refreshing}
              />
            )}
          </main>
        </div>

        <BubbleLayer bubbles={bubbles} />

        {settingsOpen && (
          <SettingsDialog
            token={token}
            onSave={(next) => {
              writeStore(TOKEN_KEY, next)
              setToken(next)
              setSettingsOpen(false)
              say('连接设置已更新', 'success')
            }}
            onClose={() => setSettingsOpen(false)}
          />
        )}
      </div>
    </div>
  )
}
