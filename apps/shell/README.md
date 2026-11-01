# 灵鲸鲸桌面壳（apps/shell）

**Tauri 2 + React 19 + TypeScript + Vite** 的桌面客户端。task-20 做了**推倒重写**：
`src/` 下所有旧组件、样式与页面逻辑已删除，从零重建。

视觉方向：暗色玻璃拟态 + 渐变光晕 + 微噪点；布局以 [docs/ui-v4-layout.md](../../docs/ui-v4-layout.md)（主人手绘线框）
为最终依据；接口契约以 [docs/api-v4.md](../../docs/api-v4.md) 为准。

---

## 一、目录结构

```text
apps/shell/src/
├── App.tsx                  # 应用壳：顶栏 / 五标签 / 左细栏 / 页面路由 / 状态轮询
├── main.tsx
├── styles.css               # 设计令牌 + 全部样式与关键帧
├── types.ts                 # 与 api-v4 契约一一对应的类型
├── icons/
│   ├── index.tsx            # 自绘 SVG 图标（导航 / 操作 / 品牌标记）
│   └── quirk.tsx            # 状态页小图标
├── lib/
│   ├── api.ts               # HTTP 层、错误翻译、状态归一化、部署配置端点
│   ├── chat.ts              # 纯逻辑：轮次分组 / 摘要 / 跟随判定
│   └── storage.ts           # localStorage 缓存 + 部署模式解析
├── components/
│   ├── Splash.tsx           # 初始化动画（粒子汇聚 + 光波扩散，可跳过）
│   ├── Onboarding.tsx       # 首次启动三步引导
│   ├── WhaleAvatar.tsx      # 圆形鲸鱼头像（呼吸 + 眨眼）
│   └── StatusRail.tsx       # 左侧 40px 状态徽章细栏
└── pages/
    ├── ChatPage.tsx         # 聊天 + 轮次卡片 + 右侧定位条
    ├── StatePage.tsx        # 互动（状态环 + 摸头/投喂等动作）
    ├── GalleryPage.tsx      # 历史画作
    ├── ToolsPage.tsx        # 小鲸鱼的工具
    ├── SettingsPage.tsx     # 设置（通用 / 模型 / 关于）
    └── ModelsSection.tsx    # 模型提供商（并入设置页）
mock-v4-server.py            # 按 api-v4 契约实现的本地 mock（仅自测用）
```

---

## 二、布局（依据 docs/ui-v4-layout.md）

```text
┌──────────────────────────────────────────────────────┐
│ ● 灵鲸鲸  在线                              (鲸鱼头像) │  顶栏：左名字，右圆形头像（点击静音）
├──────────────────────────────────────────────────────┤
│  聊天  │  互动  │ 历史画作 │ 小鲸鱼的工具 │  设置    │  五标签等宽胶囊
├────┬─────────────────────────────────────────────────┤
│在线│                                                 │
│87% │            主内容区（各页面）                    │  左侧 40px 状态徽章
│开心│                                                 │
│ 饱 │                                                 │
├────┴─────────────────────────────────────────────────┤
│  [ 输入框                              ]  发送      │  底部固定，不随内容滚动
└──────────────────────────────────────────────────────┘
```

- **顶栏**：左侧鲸鱼名字（设置里可改，存 localStorage）；右侧圆形头像用自绘 SVG，
  带呼吸（`waBreathe`）与随机眨眼（JS 触发 `.is-blinking`，动画是 CSS 的 scaleY 压扁），点击切换静音。
- **五标签**：等宽胶囊，当前项渐变描边 + 微光。
- **左侧细栏**：在线 / 能量 / 心情 / 饥饿 / 静默 五类徽章，hover 向右弹 tooltip（避免被 40px 窄条裁切），点击跳「互动」页。
- **底部发送栏**：`.composer` 是 `.page` 的 flex 子项且 `flex: 0 0 auto`，**不在滚动容器内**，因此永不滚动。

---

## 三、关键实现与取舍

### 1. 消息被吞的修复

根因是**滚动容器的高度链断裂**：只要任一祖先缺少 `min-height: 0`，`overflow-y: auto` 的容器就会按内容撑开，
外部再裁切，看起来就像「旧消息被吃掉」。

修复要点：

```css
.body        { display: flex; flex: 1; min-height: 0; }
.main        { display: flex; flex: 1; flex-direction: column; overflow: hidden; }
.page        { display: flex; flex: 1; flex-direction: column; min-height: 0; }
.chat-layout { display: flex; flex: 1; min-height: 0; }
.chat-scroll { display: flex; flex: 1; min-height: 0; overflow-y: auto; flex-direction: column; }
```

实测（760×800 视口，8 轮历史）：`scrollHeight=1212`、`clientHeight=452`、`scrollTop=760`（`atBottom: true`），
说明容器可正确滚动到底部，历史完整可达。

### 2. 跟随与停止跟随

- 用户贴近底部（距底 ≤ 80px）时，新消息到达自动滚到底；
- 一旦上滚就停止跟随，并显示「回到底部」按钮；
- 用 `useLayoutEffect` 在 DOM 更新后滚到底，避免闪一下；
- 首次加载历史后只自动滚一次（`didInitialScroll`），翻页加载更早记录时不会把用户拽走。

### 3. 轮次分组

契约明确规定「后端不预分组」。`lib/chat.ts` 的 `groupRounds()` 是纯函数，处理三种边界：
连续两条 user（上一轮无回复）、assistant 出现在任何 user 之前、最后一条 user 尚未收到回复。

### 4. 右侧轮次定位条

每轮一个小横条；hover 显示 `#序号 + 摘要`；点击 `scrollTo({behavior:'smooth'})` 定位；
滚动时按「哪张卡片的 `offsetTop` 最接近当前 `scrollTop`」判定当前轮并高亮。

> 这里刻意**不用 IntersectionObserver**：面板高度只有几百像素，Observer 的阈值在小尺寸下抖动明显。

### 5. 画廊抽搐的修复

三道保险，**完全不做基于图片自然尺寸的高度估算**：

1. 卡片 `.art { aspect-ratio: 1/1 }` —— 高度在图片加载前就已确定；
2. 图片 `position: absolute; inset: 0` —— 不参与布局；
3. `img.decode()` 成功后才加 `.is-loaded` 做 `opacity` 淡入。

实测（9 张卡）：高度全部 194px、宽度全部 194px、`aspect-ratio: 1/1`、**heightSpread = 0**、9/9 图片已淡入。

### 6. 状态环形指标：为什么不用 SVG

最初用 SVG `stroke-dasharray` 画环，实测**彩色弧段完全不渲染**。排查后发现两个坑：

- 父级带 `filter: drop-shadow` 时，`svg { transform: rotate(-90deg) }` 会与合成层冲突；
- 一旦用 `@property` 注册自定义属性（如 `--p`），父元素设置该变量后 `conic-gradient` 整段消失。

最终改用 **CSS conic-gradient**（不注册 `@property`），并单独隔离验证过它能正确渲染。
代价：数值变化是瞬时切换而非补间——**宁可要正确渲染**。这条已写进 `styles.css` 注释，避免以后有人「优化」回去。

### 7. 动画与无障碍

- 全部动画只操作 `transform / opacity / filter`，不触发布局重排；无任何 3D 库、无 canvas。
- Splash：12 个粒子 + 3 圈光波，纯 CSS；**点击 / 任意键 / 按钮均可跳过**；`prefers-reduced-motion` 下不播粒子与光波并缩短停留。
- 眨眼、Splash 跳过等 JS 侧逻辑同样读取 `prefers-reduced-motion`，不止 CSS 降级。
- `:focus-visible` 统一焦点环；图标全部 `aria-hidden`，按钮带 `aria-label`；环形用 `role="progressbar"` + `aria-valuetext`。

### 8. 图标体系

全部自绘 SVG（24×24 视口、stroke 1.6、round 端点、继承 `currentColor`），**不使用 emoji 当图标**——
emoji 由系统字体渲染，各平台形状与颜色不一致，也无法做主题化。

---

## 四、接口对接

认证头 `X-Ling-Token`。连接地址解析顺序（契约 §4）：**URL query > localStorage > 默认**。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/v1/health` | 连接检测（20s 轮询） |
| GET | `/v1/state` | 状态（字段全可选，兼容 `fatigue`/`tiredness`、`in_quiet_hours`/`quiet`） |
| POST | `/v1/chat` | 对话 |
| GET | `/v1/chat/history` | 历史（契约 §1） |
| POST | `/v1/state/interact` | 互动 |
| GET | `/v1/artwork` | 作品列表 |
| GET | `/v1/artwork/{id}/file` | 作品图片（需鉴权头，故走 fetch→blob） |
| GET/PUT | `/v1/deployment` | 部署配置（task-22） |
| GET/POST/PUT/DELETE | `/v1/providers` | 提供商 CRUD |
| POST | `/v1/providers/{id}/test` | 连通性测试 |
| POST | `/v1/providers/routing` | single / concurrent 切换 |

### 部署配置的持久化（重要）

**后端 `/v1/deployment` 是主存储**；localStorage 只作为端点不可用时的缓存兜底。
当 `/v1/deployment` 返回 404 或网络失败时，设置页会显示橙色提示「部署配置只保存在本机」，
**不会假装已经同步**。落盘到 `data/deployment.json` 由后端 / launcher 负责，前端不直接写文件。

### 首次启动引导

三步单选：是否有服务器 → 终端 / 服务器 → （服务器模式）填 base_url + token。
结果写 localStorage 并 PUT 到 `/v1/deployment`；之后可在「设置 → 通用 → 部署模式」修改。

### 模型页（并入设置）

列表 / 创建 / 编辑 / 删除 / 测试连通 / single|concurrent 切换 / 最多启用 4 个（超限直接拒绝并提示）/
分配结果展示（对话 / 记忆提取 / 生图 / 语音）。
`api_key` 严格按契约：GET 只返回 `has_api_key`；更新时「留空 = 不修改、null = 清除」。

---

## 五、验证清单

> 本机**没有 Rust 工具链**。凡涉及原生窗口、托盘、透明效果的行为一律标为**未验证**，
> 本文档任何地方都不声称它们已通过。

### ✅ 已验证（本机实际跑过）

| # | 项目 | 证据 |
| --- | --- | --- |
| 1 | `tsc --noEmit` | 退出码 0 |
| 2 | `pnpm run build` | 成功，**无警告**（js 283.16 kB / css 35.61 kB） |
| 3 | **滚动容器** | 760×800 视口、8 轮历史：`scrollHeight=1212 / clientHeight=452 / scrollTop=760`，`atBottom=true` |
| 4 | **回到底部** | 上滚后按钮出现（`toBottomVisible: true`） |
| 5 | **轮次定位条** | 渲染 8 条小横条，当前轮有 `is-active` |
| 6 | **画廊不抽搐** | 9 张卡宽高均为 194px、`aspect-ratio: 1/1`、**heightSpread = 0**、9/9 图片解码后淡入 |
| 7 | 部署配置端点 | mock 上验证 GET / PUT / 422 校验；设置页正确显示「已同步到服务器」 |
| 8 | 多视口截图 | 560 / 760px 下逐一截图核对：splash、引导、五标签（聊天/互动/历史画作/工具/设置） |

### ⚠️ 用 mock 验证，**不是**真实后端

第 3–7 项跑在 `mock-v4-server.py`（严格按 api-v4 契约实现）上，因为**后端此时还没有** `/v1/providers`、
`/v1/chat/history`、`/v1/deployment`（task-19 / task-22 并行中）。
**mock 通过 ≠ 真实后端通过**，这些点必须在后端就绪后复测。
另：绘画作品是 mock 生成的纯色 PNG，仅用于验证加载链路，不代表真实生图效果。

### ❌ 未验证

| # | 项目 | 说明 |
| --- | --- | --- |
| 1 | `cargo check` | **未运行**（cargo 不在 PATH） |
| 2 | `cargo build` / `tauri build` | **未运行**；`main.rs`、`Cargo.toml`、`capabilities` 均未经编译验证，**可能有编译错误** |
| 3 | **透明窗口** | **未验证**：只做到「配置了 `transparent: true`」，不代表合成器实际表现 |
| 4 | 托盘图标与菜单 | **未验证** |
| 5 | 窗口点穿 / 原生拖拽 | **未验证** |
| 6 | 真机 GPU 帧率 | **未验证**：截图与探测跑在无头 Edge（`--disable-gpu`），只能证明动画在跑、布局正确，**不能证明流畅度** |
| 7 | 与真实后端联调 | **未做**：`/v1/providers`、`/v1/chat/history`、`/v1/deployment` 后端尚未实现 |
| 8 | 语音 / 表情包接入 | **未做**：工具页如实标为「即将开放」，不是可点不生效的假按钮 |

---

## 六、本地自测

```bash
cd apps/shell
../../.venv/Scripts/python.exe mock-v4-server.py      # 起 mock（默认 7190）
pnpm run build
npx vite preview --host 127.0.0.1 --port 1420
# 浏览器打开：
# http://127.0.0.1:1420/?LING_BRAIN_URL=http://127.0.0.1:7190&view=settings
```

`?view=` 支持 `chat / interact / gallery / tools / settings`，便于截图与联调。

装好 Rust 后请补跑：

```bash
cargo check --manifest-path src-tauri/Cargo.toml   # 先确认能编译
npm run tauri dev                                  # 再实机验透明、托盘、点穿
```
