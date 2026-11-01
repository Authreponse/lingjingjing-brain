# 灵鲸鲸桌面 launcher

入口是 `apps/launcher/main.py`，仅使用 Python 3.11 标准库，不依赖 pywebview。

## 启动

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe apps\launcher\main.py
# launcher 退出时同时停止它自己拉起的后端：
.\.venv\Scripts\python.exe apps\launcher\main.py --stop-backend
```

## 部署模式

launcher 只读 `data/deployment.json`，首次启动引导由前端负责写入：

```json
{"mode": "local|terminal|server", "server_url": "", "token": ""}
```

环境变量 `LING_DEPLOY_MODE` 可覆盖文件中的 `mode`。配置文件缺失时明确记录日志并按 `local` 处理；JSON 无效、模式未知或 server 配置不完整都会报错，不会静默换模式。

- `local`：检测/拉起本机后端，执行下述 runtime.json + health 双保险。
- `terminal`：不拉起后端，只用开发令牌探测 `127.0.0.1:7100/v1/health`；未就绪时报“请先启动后端”。
- `server`：不拉起或探测本地后端，直接把 `server_url + token` 注入前端。server_url 必须是有效的 http(s) 地址，token 不能为空。

## local 启动顺序

1. 检测 `data/runtime.json` 与带 `X-Ling-Token` 的 `/v1/health`；若已有健康后端则直接使用，且不接管其进程。
2. 否则通过 `python -m lingbrain` 拉起独立后端进程，输出写入 `data/logs/brain.log`。
3. 默认最多等待 30 秒。正常握手要求 runtime.json 可解析且健康接口返回 HTTP 200；后端由 launcher 拉起时还会核对 runtime.json 的 pid，避免使用旧文件。
4. 后端 task 尚未写 runtime.json 时，明确记录降级日志，并使用 `127.0.0.1:7100` 与开发令牌 `lingjingjing-local-dev` 轮询健康接口。无效 JSON 也会报错并记录降级，不会静默伪装成正常握手。
5. 有 Tauri release 产物时启动 exe，通过 `LING_BRAIN_URL` / `LING_BRAIN_TOKEN` 注入；否则若 `apps/shell/dist/index.html` 存在，在回环地址随机端口启动临时静态服务，并使用系统默认浏览器打开带 `?brain_url=...&token=...` 的 URL。
6. 两种产物都没有时，明确记录降级并打开 `http://127.0.0.1:1420`（需要前端开发服务器）。

静态服务仅在 launcher 存活期间运行，绑定的仅是 `127.0.0.1`；其 HTTP 访问日志被关闭，避免把带 token 的 query 写入日志。launcher 自身日志位于 `data/logs/launcher.log`，Tauri 输出位于 `data/logs/shell.log`。

## 退出与崩溃自愈

- 默认保留后台后端；`--stop-backend` 只终止**本次 launcher 自己拉起**的进程，不会终止检测到的既有服务。
- 自己拉起的后端在运行中退出时自动重启一次并记录警告；第二次退出，或重启后不能恢复健康检查，launcher 报错并非零退出。
- 浏览器模式用 Ctrl+C 结束 launcher；Tauri 模式在前端进程退出后结束 launcher。两种模式都会关闭临时静态服务。

## Windows 打包说明（不自动打包）

提供 `lingjingjing-launcher.spec`，仅打包 launcher，不把 Python 后端或前端构建产物隐匿进 exe。先在开发环境安装 PyInstaller，再执行：

```powershell
.\.venv\Scripts\python.exe -m PyInstaller apps\launcher\lingjingjing-launcher.spec --noconfirm
```

将生成的 `dist/lingjingjing-launcher.exe` 放到项目根目录，并保留以下外部结构：

```text
apps/brain/src/lingbrain/
apps/shell/dist/                 # 浏览器模式
apps/shell/src-tauri/target/release/  # 可选 Tauri 模式
.venv/Scripts/python.exe         # 已安装后端依赖的 Python 3.11+
```

冻结后默认根目录是 exe 所在目录。若后端解释器不在默认位置，显式传入：

```powershell
.\lingjingjing-launcher.exe --python .\.venv\Scripts\python.exe
```

本任务只提供 spec 与说明，未实际执行 PyInstaller 打包，也不把未打包的结果宣称为已验证安装包。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_launcher.py -q
```

测试使用假健康探针和假后端进程，覆盖就绪双条件、降级、超时诊断、runtime 解析、URL 注入、一次重启与停止语义；不访问真实网络或真实后端。
