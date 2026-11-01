# 灵鲸鲸 V3 路线图（2026-11-01 起，工程化落地版）

> V2 完成了“统一模型接口层 + 配置留空即降级”，V3 目标：把接口层接上真实能力（CI/CD、原生桌面端、真实生图/语音/QQ/看屏/记忆 v2），形成可在服务器一键部署的完整交付物。

> 核心约束不变：本地开发环境不部署模型，所有 AI 能力走 API 接口，真实模型部署留给稳定服务器环境。

---

## V3 核心目标（按优先级，聚焦本地可完成的工程化落地）

| 优先级 | 能力 | 当前状态 | V3 目标（本地可完成） | 验收标准 |
| --- | --- | --- | --- | --- |
| P0 | GitHub Actions CI/CD | 无 | pytest + tsc + tauri build + docker build 自动化 | PR 自动跑测试+构建，合并自动打 Tag，制品上传 |
| P0 | Tauri 原生桌面宠物代码就绪 | 浏览器网页 | Rust + MSVC 编译通过，透明窗口/托盘/拖拽/系统托盘菜单代码完善 | cargo build --release 通过，结构支持透明/托盘/拖拽/菜单 |
| P0 | ComfyUI / 硅基流动 API 客户端 | Mock 占位 | 真实生图 HTTP 客户端 + 风格预设 + 队列/预算/熔断/重试 | 接口就绪，配上 API Key 即可出真图 |
| P0 | NapCat QQ 机器人接入 | 零进度 | OneBot 11 / NapCat WebSocket 适配 + 消息队列/风控/去重/主动推送 | 代码就绪，配上 NapCat 实例即可收发 |
| P1 | 记忆理解升级 v2 | 规则+模型抽取 | Chroma 向量库集成 + 知识图谱骨架 + 情感权重/衰减曲线 | 能回答“上个月聊过什么”、情感衰减曲线可配置 |
| P1 | faster-whisper + edge-tts 客户端 | Mock 音频 | ASR/TTS HTTP 客户端代码就绪（不跑模型），接口契约完善 | 接口就绪，配上服务即可跑通 |
| P1 | 真实看屏骨架 | Mock 不可用 | Windows BitBlt/DXGI Desktop Duplication + OCR API 客户端骨架 | 接口就绪，配上 OCR 服务即可描述屏幕 |
| P2 | Home Assistant / MQTT 智能家居 | 零进度 | HA REST/WebSocket + MQTT 客户端骨架 | 接口就绪，配上 HA 实例即可控制 |
| P2 | NapCat 主动推送/群消息/风控 | 零进度 | 消息队列/频控/去重/敏感词过滤/主动推送 | 代码就绪，配上实例即可用 |
| P3 | 记忆 v2：Chroma 向量库 + 知识图谱骨架 + 情感权重 | 规则+模型抽取 | Chroma 集成 + 知识图谱骨架 + 情感权重/衰减曲线 | 能回答“上个月聊过什么” |

---

## 明确不在本地做的事（同上 V2）

| 项目 | 原因 | 替代方案 |
| --- | --- | --- |
| 部署 Ollama / 本地跑 7B 模型 | 8GB 显存不够，开发环境非稳定服务器 | 接口层已就绪，配 .env 指向远程 API |
| 部署 ComfyUI / 本地跑 SDXL | 同上 | 接入硅基流动 / ComfyUI 远程 API |
| 部署 faster-whisper / Piper 本地推理 | 同上 | 接入 edge-tts / 硅基流动 TTS API / 远程 faster-whisper |
| 本地跑向量数据库重度写入 | 磁盘/内存压力大 | 本地跑 Chroma 轻量测试；生产用 Qdrant/Weaviate 集群 |

---

## 立即开工任务（按顺序）

| 任务 | 模块 | 验收标准 |
| --- | --- | --- |
| 1. GitHub Actions CI/CD | scripts/ci/ | pytest + tsc + tauri build + docker build 自动化；PR 自动跑测试+构建，合并自动打 Tag，制品上传 |
| 2. Tauri 编译代码完善 | apps/shell/src-tauri/ | cargo build --release 通过，结构支持透明窗口/托盘/拖拽/系统托盘菜单 |
| 3. ComfyUI / 硅基流动 HTTP 客户端 | image_gen/ + api_image | 接口就绪，配上 API Key 即可出真图 |
| 4. NapCat QQ 机器人 WebSocket 适配 | qq_bot/ | OneBot 11 / NapCat WS 适配 + 消息队列/风控/去重/主动推送 |
| 5. 记忆 v2：Chroma 向量库集成 + 知识图谱骨架 + 情感权重 | memory_v2/ | 能回答“上个月聊过什么”，情感衰减曲线可配置 |
| 6. GitHub Actions CI：pytest + tsc + tauri build + docker build | scripts/ci/ | PR 自动跑测试+构建，合并自动打 Tag |

---

## 交付物结构（V3 新增/修改）

apps/
  brain/
    src/lingbrain/
      model_providers/      # 已完成
      memory_v2/            # 进行中
      voice/                # 进行中
      image_gen/            # 进行中
      screen_capture/       # 进行中
      qq_bot/               # 进行中
      home_assistant/       # 进行中
  shell/                        # Tauri 原生重构
    src-tauri/                # 进行中
    src/                      # React 前端
scripts/
  ci/                           # 进行中 (GitHub Actions)
  deploy/                       # 打包/部署脚本
docker/
  docker-compose.yml            # 一键部署（服务器用）

---

## 验收清单（Definition of Done - V3 本地版）

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
| M3.1 | 2026-11-15 | CI/CD 通 + Tauri 编译通过 + 记忆 v2 原型 + 生图客户端就绪 |
| M3.2 | 2026-11-30 | Tauri 编译通过 + NapCat QQ 接入代码就绪 + ComfyUI/API 客户端就绪 |
| M3.3 | 2026-12-15 | 记忆 v2 原型 + 看屏接口骨架 + HA/MQTT 适配骨架 |
| V3.0 Code Ready | 2026-12-31 | 所有接口代码就绪、CI/CD 完善、文档同步，可在服务器一键部署跑通 |

---

## 推进方式

每个任务按竖切推进：
1. 先写最薄的端到端接口契约（Mock 模式可跑通）
2. 补单测、补文档、跑回归
3. 合并主分支、打 Tag、更新当前状态.md
4. 下一个任务

> 记录于 2026-11-01，V3 启动。