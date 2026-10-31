# v1 运维与 M0 竖切脚本

所有脚本仅使用 **Python 3.11 标准库**，支持 Windows、macOS 和 Linux。默认项目根目录是脚本所在 `scripts/` 的上一级，与调用命令时的当前目录无关：

- 数据库：`<项目根>/data/lingjingjing.db`
- 备份：`<项目根>/backups/`

Windows 示例用 `py -3.11`；其他平台可替换为 `python3.11`。下列命令均在项目根目录执行。

## 1. 备份 `backup.py`

```powershell
py -3.11 scripts/backup.py
py -3.11 scripts/backup.py --label nightly --retention-days 14
```

使用 `sqlite3.Connection.backup()`，先写临时文件，验证 `PRAGMA integrity_check` 和源/目标页数后再原子发布。JSON 输出包含备份路径、UTC 时间、字节数、页数、SHA-256、完整性结果和清理列表。

默认保留 14 天，只清理由本脚本命名且过期的 `.db`（以及对应升级清单），不会删除备份目录中的任意文件。`--no-prune` 可禁止本次清理。可用 `--database`、`--backup-dir` 覆盖默认路径。

## 2. 恢复 `restore.py`

```powershell
# 目标不存在：可直接恢复
py -3.11 scripts/restore.py backups/lingjingjing-...-backup.db

# 目标存在：必须显式 --force
py -3.11 scripts/restore.py backups/lingjingjing-...-backup.db --force
```

默认拒绝覆盖任何现有数据库。`--force` 下先通过 SQLite backup API 创建 `pre-restore` 安全备份，安全备份成功后才原子替换目标；恢复源、暂存文件和最终文件均校验完整性及逐表行数。

**先正常停止所有数据库写入方。** 为避免旧 WAL 重放到新文件，目标旁存在 `-wal` 或 `-shm` 时脚本会拒绝替换。正常停止 SQLite 应用通常会自动清理这些文件；不要直接删除仍在使用的 WAL。失败信息会指出安全备份或失败原因，任何校验失败均返回非零退出码。

## 3. 升级前快照 `upgrade.py`

```powershell
py -3.11 scripts/upgrade.py --version v1.0.0
```

脚本输出冻结建议，创建带 `pre-upgrade-v1.0.0` 标签的快照，并生成 `<快照>.manifest.json`。校验包括：

- SQLite 完整性；
- 源库与快照逐表行数及总行数；
- 非空文件大小，且快照大小等于 `page_count × page_size`；
- 源库快照前后大小/mtime 稳定，快照 mtime 不早于源库；
- 快照版本标签和 SHA-256 清单。

WAL 模式下源主文件大小可以合法地不同于快照，因此不强行要求二者字节数相等。任一校验失败仍保留快照和失败清单用于诊断，并以非零状态退出。

本脚本**只负责升级前快照，不执行应用迁移**。应先冻结写入并确认回滚方案，再运行迁移。

## 4. M0 竖切 `vertical_slice_check.py`

默认 API 为 `http://127.0.0.1:7100`，认证头为 `X-Ling-Token`，默认开发令牌 `lingjingjing-local-dev`。可用 `--token` 或环境变量 `LING_TOKEN` 覆盖；`--base-url` / `LING_BASE_URL` 覆盖地址。

```powershell
# 核心 HTTP 检查；不明确执行重开
py -3.11 scripts/vertical_slice_check.py --skip-restart

# 提供完整的“停止并启动服务”命令；命令返回后脚本检查持久性
py -3.11 scripts/vertical_slice_check.py --command "你的重启命令"
```

实际检查：

1. `GET /v1/health` 健康；
2. 聊天前记录 `GET /v1/events?limit=...` 基线；
3. `POST /v1/chat`，JSON 为 `{message, client_id}`，发送带唯一标记的中文记忆内容，并要求中文回复而非纯请求回显；
4. 轮询事件接口，要求出现与本次标记、客户端或聊天/记忆类型相关的新事件；
5. 轮询 `GET /v1/memory/search?q=...`，先查“喜欢”，再查本次实际写入的“桂花乌龙”；支持 `memories/items/results/data` 等常见嵌套返回，并提取实际中文记忆内容；
6. `--command` 模式执行用户给出的完整重启命令，等待健康恢复、请求 `GET /v1/state`，再用重启前的实际记忆内容（必要时回退原查询词）确认同一条记忆仍存在。

`--command` 必须自行完成停止与启动，并在启动动作发起后返回；不要传入一直占据终端的前台服务进程。Windows 下由系统 shell 执行，路径和引号按 `cmd.exe` 规则书写。重启超时由 `--restart-timeout` 控制。

未提供 `--command` 或指定 `--skip-restart` 时，重开持久性为 **SKIP**。显存和真实 Tauri 桌面环境无法由该纯 HTTP 脚本证明，始终明确为 **SKIP**，不会伪造 PASS。任何核心检查 FAIL 都使退出码非零；只有 SKIP 的可选环境检查不会令核心 HTTP 竖切失败。

## 本地测试辅助

```powershell
py -3.11 -m unittest discover -s scripts -p "test_*.py" -v
```

测试使用临时 SQLite 数据库和本机临时 HTTP 服务，不需要真实大脑、GPU 或 Tauri，不访问外部网络。
