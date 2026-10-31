import { useEffect, useRef } from 'react'
import { isTauri } from '@tauri-apps/api/core'
import { listen } from '@tauri-apps/api/event'

/**
 * 托盘菜单 → 前端动作。
 *
 * 设计取舍：托盘本身由 Rust 侧 TrayIconBuilder 构建（见 src-tauri/src/main.rs），
 * 菜单项点击后只 emit 一个事件名，前端监听并执行对应的界面动作。
 * 这样菜单逻辑集中在前端一处，也避免 Rust 侧持有 React 状态。
 *
 * 未在 Tauri 中运行（浏览器预览 vite dev）、或事件通道不可用时，
 * 这里静默失败——不会抛出到界面，也不会假装托盘存在。
 */
export type TrayAction =
  | 'show'
  | 'hide'
  | 'toggle-mute'
  | 'refresh'
  | 'reset-window'
  | 'open-settings'

export interface TrayHandlers {
  show: () => void
  hide: () => void
  toggleMute: () => void
  refresh: () => void
  resetWindow: () => void
  openSettings: () => void
}

const TRAY_EVENT = 'ling://tray-action'

declare global {
  interface Window {
    __lingTray?: (action: TrayAction) => void
  }
}

export function useTrayMenu(handlers: TrayHandlers): void {
  const latest = useRef(handlers)
  latest.current = handlers

  useEffect(() => {
    const dispatch = (action: TrayAction) => {
      const current = latest.current
      switch (action) {
        case 'show':
          current.show()
          break
        case 'hide':
          current.hide()
          break
        case 'toggle-mute':
          current.toggleMute()
          break
        case 'refresh':
          current.refresh()
          break
        case 'reset-window':
          current.resetWindow()
          break
        case 'open-settings':
          current.openSettings()
          break
      }
    }

    window.__lingTray = dispatch

    if (!isTauri()) {
      return () => {
        delete window.__lingTray
      }
    }

    let dispose: (() => void) | undefined
    let cancelled = false

    void listen<string>(TRAY_EVENT, (event) => {
      const action = event.payload as TrayAction
      if (typeof action === 'string') dispatch(action)
    })
      .then((unlisten) => {
        if (cancelled) unlisten()
        else dispose = unlisten
      })
      .catch(() => {
        // 托盘事件不可用：不影响主界面。
      })

    return () => {
      cancelled = true
      dispose?.()
      delete window.__lingTray
    }
  }, [])
}
