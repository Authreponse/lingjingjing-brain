#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! 灵鲸鲸桌面壳的 Tauri 入口。
//!
//! 负责两件界面层面的事：
//! 1. **托盘常驻**：右键托盘图标显示菜单（显示/隐藏、静音、刷新、回到屏幕中间、设置、退出）。
//!    菜单项不直接操作 React 状态，只 emit 一个事件给前端（见 src/useTrayMenu.ts），
//!    由前端统一处理，保证「托盘点一下」和「界面上点一下」走同一套逻辑。
//! 2. **点穿（click-through）**：静音时调用 set_click_through(true)，
//!    让窗口不拦截鼠标，桌宠可以安静地待在别的窗口上面而不挡操作。
//!
//! ⚠️ 构建说明：本文件需要 Rust 工具链 + Windows 平台依赖才能编译（cargo check / tauri build）。
//!    在**没有 Rust 的环境里前端仍可独立构建**（pnpm run build 只跑 tsc + vite），
//!    但托盘与点穿必须在装了工具链的机器上实机验证后才能算「已验证」。

use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Emitter, Manager,
};

/// 托盘菜单点击后发给前端的事件名。
const TRAY_EVENT: &str = "ling://tray-action";

/// 主窗口的 label，与 tauri.conf.json 中 windows[0].label 一致。
const MAIN_WINDOW: &str = "main";

/// 把托盘动作转发给前端。
fn emit_tray_action(app: &AppHandle, action: &str) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        // 前端若因为窗口隐藏/销毁没在监听，这里失败也无所谓：托盘自身的显隐逻辑已生效。
        let _ = window.emit(TRAY_EVENT, action);
    }
}

/// 切换窗口的「鼠标点穿」。返回切换后的点穿状态。
///
/// 静音时忽略鼠标事件，桌宠不会挡住下面的操作；恢复时重新接收点击。
#[tauri::command]
fn set_click_through(window: tauri::WebviewWindow, through: bool) -> Result<bool, String> {
    window
        .set_ignore_cursor_events(through)
        .map_err(|err| format!("切换点穿失败：{err}"))?;
    Ok(through)
}

fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let show = MenuItem::with_id(app, "show", "显示灵鲸鲸", true, None::<&str>)?;
    let hide = MenuItem::with_id(app, "hide", "隐藏到托盘", true, None::<&str>)?;
    let mute = MenuItem::with_id(app, "toggle-mute", "静音 / 恢复", true, None::<&str>)?;
    let refresh = MenuItem::with_id(app, "refresh", "刷新状态", true, None::<&str>)?;
    let reset = MenuItem::with_id(app, "reset-window", "回到屏幕中间", true, None::<&str>)?;
    let settings = MenuItem::with_id(app, "open-settings", "令牌设置", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "退出灵鲸鲸", true, None::<&str>)?;

    let separator_one = PredefinedMenuItem::separator(app)?;
    let separator_two = PredefinedMenuItem::separator(app)?;

    let menu = Menu::with_items(
        app,
        &[
            &show,
            &hide,
            &separator_one,
            &mute,
            &refresh,
            &reset,
            &settings,
            &separator_two,
            &quit,
        ],
    )?;

    let mut builder = TrayIconBuilder::with_id("main-tray")
        .tooltip("灵鲸鲸 · 桌面小伙伴")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => {
                if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
                    let _ = window.show();
                    let _ = window.unminimize();
                    let _ = window.set_focus();
                }
                emit_tray_action(app, "show");
            }
            "hide" => {
                if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
                    let _ = window.hide();
                }
                emit_tray_action(app, "hide");
            }
            "quit" => {
                app.exit(0);
            }
            other => emit_tray_action(app, other),
        })
        .on_tray_icon_event(|tray, event| {
            // 左键单击直接唤起窗口（不用再去点菜单）。
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                let app = tray.app_handle();
                if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
                    let _ = window.show();
                    let _ = window.unminimize();
                    let _ = window.set_focus();
                }
            }
        });

    // 复用打包图标作为托盘图标，避免额外资源。
    if let Some(icon) = app.default_window_icon().cloned() {
        builder = builder.icon(icon);
    }

    builder.build(app)?;
    Ok(())
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![set_click_through])
        .setup(|app| {
            if let Err(err) = build_tray(app.handle()) {
                // 托盘创建失败不应导致桌宠起不来：记日志后继续。
                eprintln!("创建托盘图标失败：{err}");
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("启动灵鲸鲸桌面壳失败");
}
