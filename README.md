# 灵鲸鲸

一个养在本地、有事件记忆、有状态的独立个体。三个进程即便同机也只通过网络接口通信，不把未来迁移写死成进程内调用。

> **想知道现在到底做到哪一步？先读 [当前状态.md](当前状态.md)。**
> 那一页按立项书的验收标准逐条打分，明确写了哪些达标、哪些半成、哪些完全没做。

## 当前状态

- **M0 已完成**：聊天 API、追加式事件日志、SQLite 持久化、中文事实记忆检索、状态与心跳、WebSocket。已实测杀进程重启后记忆仍在。
- **M1 已完成**：后端状态机（能量/饱腹/疲劳随时间衰减、睡眠恢复、静默时段、拖拽互动、心情与在场投影）；桌宠壳升级（状态气泡、能量条、托盘、离线兜底、拖拽互动）。
- **M2 已完成**：GPU 单卡互斥排队器、生图服务、artwork 落库追溯、每日预算、表情包系统。
- **运维脚本**：备份、恢复、升级前快照、竖切验收。
- **开发模型驱动**：默认 `echo` 仅用于无模型验收，不是真实 AI。实际聊天请切换 Ollama 或其他 OpenAI 兼容服务。
- **M3 已完成**：ASR/TTS 后端协议 + Mock 实现、一级按住说话全链路（音频→转写→对话→语音回复）。
- **M4 已完成**：视频观察（搜索/元数据/字幕/评论/准入规则/不可信材料隔离）、私下分享队列（1-3条/天≤3批/只发链接+评价）、观点存档。
- **M5 已完成**：静态看屏（默认关闭、暂停键持久、黑名单截前拦截、可见提示、不替主人操作）。
- **UI**：桌宠壳已重构（鲸鱼生命感动画、环形+波浪状态可视化、瀑布流画廊、打字机、响应式），React + Tauri 2，纯 CSS 动画无新增依赖。
- **能力接口一律"留空即降级"**：任何需要模型的能力不配置就走占位并明确告知，接哪个填哪个。见 `.env.example` 与 `GET /v1/capabilities`（当前 0/5 已配置，属预期）。
- **记忆理解升级**：保留规则提取作兜底，新增模型结构化抽取（降级标 `rule-fallback`、带 `source_event_id` 可追溯、含"编造闸"防止模型编造原文没有的内容）。
- **主动话题**：每天 ≤3 次、间隔 ≥90 分钟、安静时段静默、没有真实话题就不发（不拼"在吗"凑数）。
- **真实 B 站抓取**：实测修正了评论接口与 aid/cid 取值错误，风控 412/-352/登录态分类处理，关键帧用 ffmpeg 真抽帧。
- **能力接口一律留空即降级**：任何需要模型的能力不配置就走占位并明确告知，接哪个填哪个，见 `.env.example` 与 `GET /v1/capabilities`。
- **记忆理解升级**：保留规则提取作兜底，新增模型结构化抽取（降级标 `rule-fallback`，带 `source_event_id` 可追溯，含"编造闸"）。
- **主动话题**：每天 ≤3 次、间隔 ≥90 分钟、安静时段静默、无话题就不发。
- **真实 B 站抓取**：实测修正评论接口与 aid/cid 取值，风控/登录态/限频分类处理，关键帧用 ffmpeg 真抽帧。

### 哪些已验证、哪些没有（如实区分）

| 已验证（有可复现证据） | 未验证 |
| --- | --- |
| 后端 **279 项** pytest 全绿、运维 8 项全绿 | Tauri 原生构建（`cargo check`/打包）：本机无 Rust 与 MSVC |
| 真实进程端到端：中文对话、记忆检索、重启后记忆仍在 | 透明窗口、托盘图标、窗口点穿、原生拖拽手感 |
| M1 状态机 29 字段实时推进、拖拽事件生效 | 真实生图（SDXL/Flux）：本机无 ComfyUI |
| M2 排队器状态流转、预算上限、OOM 熔断、人工恢复 | 真实 GPU 显存共存（对话+视觉+生图同时驻留） |
| M2 表情包 13 种选择与「画个你自己」链路 | 真实模型的对话质量与人格表现（当前 echo 不是 AI） |
| M3 TTS 生成 44KB 真 WAV(1.38s/16kHz)、ASR 往返、voice-turn 全链路 | 真实语音模型（Whisper/Piper）：本机未装 |
| M4 分享红线五种情况全拦截（含全角＠）、长视频跳过 | B 站真实网络抓取与真实字幕/评论质量 |
| M5 未开启时 capture 返回 403、暂停键重启后仍生效 | 真实屏幕截取（RealScreenBackend 恒不可用，按设计） |
| UI 构建通过、对真实后端 58 项断言、键盘流程实测 | UI 在原生窗口中的实际观感（未打包运行） |

⚠️ 当前生图使用 **MockBackend**：它用 Pillow 画真实可解码的 PNG，但画面是**占位图形**（海面渐变 + 鲸形椭圆），**不是真正的 AI 绘画**。接入 ComfyUI 后才有真实出图。

## 目录

| 路径 | 用途 |
| --- | --- |
| apps/brain/src/lingbrain/ | 大脑：API、事件、记忆、模型路由 |
| apps/shell/ | 桌宠壳与状态外显 |
| scripts/ | 备份、恢复、升级与竖切验收 |
| docs/ | 模型、提示词、数据、权限、熔断等执行文档 |
| 灵鲸鲸的诞生/ | 给她看的文件 |
| 交接卡.md | 跨会话最小上下文 |
| data/lingjingjing.db | 默认数据库（运行时生成，不入库） |

## 第一次启动（Windows / Python 3.11）

在项目根目录执行：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m lingbrain
```

大脑地址为 http://127.0.0.1:7100，开发令牌为 lingjingjing-local-dev。生产或组网前必须修改 .env 中的 LING_AUTH_TOKEN；使用默认令牌时，程序拒绝监听非本机地址。

## 接入真实模型

以本机 Ollama 为例，先安装 Ollama 并拉取已量化模型，再修改 .env：

```dotenv
LING_MODEL_PROVIDER=openai_compatible
LING_MODEL_NAME=qwen2.5:7b-instruct-q4_K_M
LING_MODEL_BASE_URL=http://127.0.0.1:11434/v1
LING_MODEL_API_KEY=
```

模型名称必须与 ollama list 中实际名称一致。8GB 显存的加载、卸载和视觉模型共存策略见 docs/模型清单.md。

## M1 状态与 M2 生图接口

```powershell
$h = @{ "X-Ling-Token" = "lingjingjing-local-dev" }

# M1：富状态（energy/satiety/fatigue/mood/presence/hungry/tired/in_quiet_hours…）
Invoke-RestMethod -Uri http://127.0.0.1:7100/v1/state -Headers $h

# M1：拖拽互动
Invoke-RestMethod -Uri http://127.0.0.1:7100/v1/state/interact -Method Post -Headers $h `
  -ContentType "application/json" -Body '{"kind":"drag","client_id":"shell","x":100,"y":200}'

# M2：生图（返回 artwork 对象，含 id/path/prompt/reason/mood）
Invoke-RestMethod -Uri http://127.0.0.1:7100/v1/image/generate -Method Post -Headers $h `
  -ContentType "application/json; charset=utf-8" `
  -Body '{"prompt":"灵鲸鲸在月光下的海面","reason":"想要一张","mood":"平静","size":512}'

# M2：队列与预算状态
Invoke-RestMethod -Uri http://127.0.0.1:7100/v1/image/status -Headers $h

# M2：表情包
Invoke-RestMethod -Uri http://127.0.0.1:7100/v1/emotions -Headers $h
Invoke-RestMethod -Uri http://127.0.0.1:7100/v1/emotions/render -Method Post -Headers $h `
  -ContentType "application/json; charset=utf-8" -Body '{"trigger":"画个你自己","kind":"proactive"}'
```

接入真实生图（ComfyUI）时设置 `LING_COMFYUI_URL`，例如 `http://127.0.0.1:8188`；未设置则自动使用 MockBackend。

**状态字段命名以 `fatigue` / `in_quiet_hours` 为准**，新代码不要再用 `tiredness` / `quiet`，详见交接卡 3.1。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe scripts\vertical_slice_check.py
```

另开终端调用一次：

```powershell
$headers = @{ "X-Ling-Token" = "lingjingjing-local-dev" }
Invoke-RestMethod -Uri http://127.0.0.1:7100/v1/chat `
  -Method Post -Headers $headers -ContentType "application/json; charset=utf-8" `
  -Body '{"message":"我叫阿星，我喜欢蓝鲸","client_id":"desktop"}'
Invoke-RestMethod -Uri "http://127.0.0.1:7100/v1/memory/search?q=蓝鲸" -Headers $headers
```

关闭并重启大脑后再次检索，记忆仍在。完整 API 见 docs/api.md，运维命令见 scripts/README.md。

## v1 只做六件事

聊天、发消息、发图片、一级语音、看视频、看评论；分享只发链接和推荐语。v1 不在公共评论区发言、不 @ 人、不回复别人评论、不私信陌生人、不控制设备、不购物、不进大群。

## 安全与数据

- P0 只监听 127.0.0.1，不开放公网端口。
- 所有 HTTP 接口需要 X-Ling-Token；WebSocket 使用同名请求头或 token 查询参数。
- 事件表由数据库触发器禁止更新和删除；事实记忆带 source_event_id 可追溯。
- 备份、恢复和升级必须走脚本，不直接复制正在写入的 SQLite 文件。
- 数据库、生成的图片与语音、备份、`.env` 均不进入仓库（见 `.gitignore`）。
- `灵鲸鲸的诞生/` 中的两封私人信件（第一版私信、未启封的信）不随公开仓库发布，由主人私人保管。

## 许可证

本项目采用 **GNU Affero General Public License v3.0（AGPL-3.0）**，全文见 [LICENSE](LICENSE)。

简单说：你可以自由使用、修改、分发；但如果你把它作为网络服务提供给他人，你也必须公开修改后的源码。
