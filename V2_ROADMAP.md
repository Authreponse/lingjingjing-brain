# 灵鲸鲸 V2 路线图（2026-11-01 起，本地开发环境版）

> 核心约束：**本地开发环境不部署任何模型**。所有 AI 能力走 API 接口（已就绪的“留空即降级”架构），真实模型部署留给稳定服务器环境。

---

## V2 核心目标（本地可完成项优先）

| 优先级 | 能力 | 当前状态 | V2 目标（本地可做） | 验收标准 |
| --- | --- | --- | --- | --- |
| P0 | 模型接口层完善 | echo 占位 | 统一 OpenAI 兼容 / Ollama / 硅基流动等多提供商接口，统一错误码/重试/流式/工具调用 | 接口层单测 100% 通过，切换提供商零代码改动 |
| P0 | 记忆理解升级 v2 | 规则+模型抽取 | 向量检索+知识图谱骨架+情感权重（本地可跑向量库 Chroma/Qdrant） | 能回答“上个月聊过什么”、情感衰减曲线可配置 |
| P0 | GitHub Actions CI/CD | 无 | pytest + tsc + tauri build + docker build 自动化 | PR 自动跑测试+构建，合并自动打 Tag |
| P0 | Tauri 原生桌面宠物 | 浏览器网页 | Rust + MSVC 编译通过，透明窗口/托盘/拖拽代码完善（不要求本地实测） | 代码可编译出 .msi，代码结构支持透明/托盘/拖拽 |
| P1 | ComfyUI / 硅基流动 API 接入 | Mock 占位 | 真实生图 HTTP 客户端 + 风格预设 + 队列/预算/熔断 | 接口就绪，配上 API Key 即可出真图 |
| P1 | faster-whisper + edge-tts 落地 | Mock 音频 | ASR/TTS 客户端代码就绪（不跑模型），接口契约完善 | 接口就绪，配上服务即可跑通 |
| P1 | NapCat QQ 机器人接入 | 零进度 | OneBot 11 / NapCat WebSocket 适配 + 消息队列/风控/去重 | 代码就绪，配上 NapCat 实例即可收发 |
| P2 | 真实看屏 | Mock 不可用 | Windows BitBlt/DXGI Desktop Duplication + OCR 接口骨架 | 接口就绪，配上 OCR 服务即可描述屏幕 |
| P2 | 记忆理解 v2（向量+图谱） | 规则+模型抽取 | Chroma/Qdrant 向量库接入 + 知识图谱骨架 + 情感权重 | 能回答“上个月聊过什么” |
| P2 | 真实看屏 + OCR 接入 | Mock 不可用 | Windows 截屏 + OCR API 客户端（PaddleOCR/RapidOCR 客户端） | 接口就绪 |
| P3 | QQ/手机消息通道 | 零进度 | OneBot 11 / NapCat + WebSocket + 推送通知 | 代码就绪，配上实例即可收发 |
| P3 | Home Assistant / MQTT 智能家居 | 零进度 | HA REST/WebSocket + MQTT 客户端骨架 | 接口就绪 |

---

## 明确不在本地做的事

| 项目 | 原因 | 替代方案 |
| --- | --- | --- |
| 部署 Ollama / 本地跑 7B 模型 | 8GB 显存不够，开发环境非稳定服务器 | 接口层已就绪，配 .env 指向远程 API |
| 部署 ComfyUI / 本地跑 SDXL | 同上 | 接入硅基流动 / ComfyUI 远程 API |
| 部署 faster-whisper / Piper 本地推理 | 同上 | 接入 edge-tts / 硅基流动 TTS API / 远程 faster-whisper |
| 本地跑向量数据库重度写入 | 磁盘/内存压力大 | 本地跑 Chroma 轻量测试；生产用 Qdrant/Weaviate 集群 |

---

## 本周可落地任务（仅含本地可完成项）

| 任务 | 负责模块 | 验收标准 |
| --- | --- | --- |
| 1. 模型接口层重构：统一 OpenAI/Ollama/硅基流动/自定义 | model_providers/ | 提供商切换零代码改动，统一错误码/重试/流式/工具调用 |
| 2. GitHub Actions CI：pytest + tsc + tauri build | scripts/ci/ | PR 自动跑测试+构建，合并自动打 Tag |
| 3. Tauri 编译代码完善：透明窗口/托盘/拖拽/系统托盘菜单 | shell/src-tauri/ | 代码可 cargo build --release，结构支持透明/托盘/拖拽 |
| 4. ComfyUI/硅基流动 HTTP 客户端 + 风格预设/队列/预算/熔断 | image_gen/ + api_image | 接口就绪，配上 API Key 即可出真图 |
| 5. NapCat QQ 机器人 WebSocket 适配 + 消息队列/风控/去重 | qq_bot/ | 代码就绪，配上 NapCat 实例即可收发 |
| 6. 记忆 v2：Chroma 向量库集成 + 知识图谱骨架 + 情感权重 | memory_v2/ | 能回答上个月聊过什么，情感衰减曲线可配置 |
| 7. GitHub Actions CI：pytest + tsc + tauri build + docker build | scripts/ci/ | PR 自动跑测试+构建，合并自动打 Tag |

---

## 交付物结构（V2 新增/修改）

apps/
  brain/
    src/lingbrain/
      model_providers/      # 统一模型接口层
      memory_v2/            # 向量+图谱记忆
      voice/                # ASR/TTS 客户端（不跑模型）
      image_gen/            # ComfyUI/API 封装
      screen_capture/       # Windows 真实截屏骨架
      qq_bot/               # OneBot/NapCat 适配
      home_assistant/       # HA/MQTT 适配
  shell/                        # Tauri 原生重构
    src-tauri/                # Rust 代码
    src/                      # React 前端
scripts/
  ci/                           # GitHub Actions
  deploy/                       # 打包/部署脚本
docker/
  docker-compose.yml            # 一键部署（服务器用）

---

## 验收清单（Definition of Done - V2 本地版）

每个能力上线前必须通过：
- 单元测试 >= 90% 覆盖
- 集成测试：端到端接口契约跑通（Mock 模式）
- 回归测试：全量 pytest 375+ 全绿
- 代码可编译：tauri build --release 通过（不要求本地运行验证）
- 文档同步：README/当前状态.md/交接卡.md 同步更新

---

## 里程碑（本地版）

| 里程碑 | 目标日期 | 标志性交付 |
| --- | --- | --- |
| M2.1 | 2026-11-15 | 接口层完善 + CI/CD + Tauri 编译通过 + 记忆 v2 原型 |
| M2.2 | 2026-11-30 | Tauri 编译通过 + NapCat QQ 接入代码就绪 + ComfyUI/API 客户端就绪 |
| M2.3 | 2026-12-15 | 记忆 v2 原型 + 看屏接口骨架 + HA/MQTT 适配骨架 |
| V2.0 Code Ready | 2026-12-31 | 所有接口代码就绪、CI/CD 完善、文档同步，可在服务器一键部署跑通 |

---

## 推进方式

每个任务按竖切推进：
1. 先写最薄的端到端接口契约（Mock 模式可跑通）
2. 补单测、补文档、跑回归
3. 合并主分支、打 Tag、更新当前状态.md
4. 下一个任务

记录于 2026-11-01，已调整为本地不部署模型版本。