# 灵鲸鲸大脑 API v1

> 基线：Python 3.11、FastAPI、SQLite FTS5 trigram；数据库 data/lingjingjing.db。
> 默认地址：http://127.0.0.1:7100。五个固定 HTTP 契约不因客户端或模型变化而改名。

## 1. 认证与安全

所有固定端点都要求请求头：

    X-Ling-Token: lingjingjing-local-dev

lingjingjing-local-dev 是默认开发令牌，只允许配合 127.0.0.1 回环地址使用。局域网、公网或组网部署前必须由主人更换为高熵随机令牌，并使用 WireGuard/Tailscale 或 TLS；禁止直接做公网端口映射。

认证失败统一为 401。令牌不得写入 URL（WebSocket 的兼容例外见第 8 节）、前端源码、截图、event、memory 或错误日志。client_id 只是终端标识，不是认证凭证，也不能用来提升权限。

Content-Type：POST 使用 application/json；字符编码 UTF-8。时间均为带 UTC 偏移的 ISO 8601 字符串。

## 2. POST /v1/chat

接收一条主人消息，返回回答、最新状态、事件 ID 和本次使用的记忆。

请求体只有以下业务字段：

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| message | string | 是 | 去除空白后至少 1 字符，最长 4000 |
| client_id | string | 否 | 默认 desktop，1–80 字符；例如 desktop、phone、test |

请求示例：

    POST /v1/chat
    X-Ling-Token: lingjingjing-local-dev
    Content-Type: application/json

    {"message":"你还记得我喜欢什么吗？","client_id":"desktop"}

200 响应：

    {
      "reply":"记得，你喜欢蓝鲸。",
      "state":{"energy":99,"mood":"平静","presence":"在","sleeping":false},
      "user_event_id":"7f0d...",
      "assistant_event_id":"a31c...",
      "memories":[{
        "id":"m-01",
        "kind":"preference",
        "who":"主人",
        "content":"主人喜欢蓝鲸",
        "weight":0.5,
        "ts":"2026-10-31T13:00:00.000+00:00",
        "source":"conversation",
        "source_event_id":"7f0d..."
      }]
    }

服务端在同一次业务处理中保存用户事件、回答事件和可核记忆。模型不可用返回 503，输入无效返回 422。网络超时不代表服务端一定未写入，客户端重发前应先查询 events 核对。

## 3. GET /v1/state

无请求体、无查询参数。200 返回当前投影对象，而不是 key/value 行数组：

    {
      "energy":100,
      "mood":"平静",
      "presence":"在",
      "sleeping":false,
      "started_at":"2026-10-31T12:00:00.000+00:00",
      "last_heartbeat":"2026-10-31T13:00:00.000+00:00"
    }

M1 可增加 satiety、muted 等键。客户端必须容忍新增键和可选键缺失。state 可由 event 重放，客户端不能直接修改此投影。

## 4. GET /v1/health

用于存活与数据库完整性检查，也需要认证。200 示例：

    {
      "status":"ok",
      "service":"miao-brain",
      "version":"0.1.0",
      "database":"ok",
      "schema_version":1,
      "model_provider":"echo",
      "time":"2026-10-31T13:00:00.000+00:00"
    }

service=miao-brain 是沿用立项书的技术进程名，不代表与另一个项目“喵酱”共享记忆。PRAGMA integrity_check 非 ok 时返回 503；此时不得继续写库或自动升级。

## 5. GET /v1/events?limit

查询参数：

| 参数 | 类型 | 必填 | 默认与范围 |
|---|---|---|---|
| limit | integer | 否 | 默认 50，1–200 |

200 返回按 ts DESC、rowid DESC 排列的事件：

    {
      "items":[{
        "id":"a31c...",
        "ts":"2026-10-31T13:00:00.000+00:00",
        "actor":"灵鲸鲸",
        "kind":"assistant.message",
        "payload":{"content":"记得，你喜欢蓝鲸。","client_id":"desktop"}
      }]
    }

这是有界最新事件视图，不是无限分页承诺。客户端按 id 去重；渲染聊天时再转为正序。事件不可通过 API 修改或删除。

## 6. GET /v1/memory/search?q

固定查询参数：

| 参数 | 类型 | 必填 | 约束 |
|---|---|---|---|
| q | string | 是 | 1–200 字符，去空白后不能为空 |

请求：GET /v1/memory/search?q=蓝鲸

200 返回：

    {"items":[{
      "id":"m-01","kind":"preference","who":"主人",
      "content":"主人喜欢蓝鲸","weight":0.5,
      "ts":"2026-10-31T13:00:00.000+00:00",
      "source":"conversation","source_event_id":"7f0d..."
    }]}

实现使用 SQLite FTS5 trigram；少于 3 个字符的查询可用参数化 LIKE 兜底。查询词始终绑定参数，不拼接 SQL。没有命中时 items=[]，不编造“相关记忆”。

## 7. 错误约定

| 状态码 | 场景 | 客户端动作 |
|---|---|---|
| 401 | X-Ling-Token 缺失或错误 | 停止自动重试，提示主人检查配置 |
| 422 | 请求字段、类型或长度不合法 | 保留输入并显示校验信息 |
| 503 | 模型暂不可用或数据库完整性失败 | 保留草稿；按错误类型降级，不反复写库 |
| 500 | 未预期服务端错误 | 记录可排障信息，不泄露令牌或堆栈给公网终端 |

FastAPI 错误体通常为 {"detail":"..."}；422 也可能是字段错误数组。客户端必须读取状态码，不要把错误 detail 当作灵鲸鲸的回答展示。

## 8. 当前实现扩展（不替代五个固定契约）

Lead 已确认保留下列现有功能。它们是最低固定契约之外的扩展，客户端只使用五个固定端点也必须能工作。

### 8.1 /v1/memory/search 的可选 limit

除必填 q 外，当前实现另接受可选整数 limit：默认 10，范围 1–50。响应结构不变。未传 limit 时行为完全符合第 6 节。

### 8.2 WebSocket /v1/ws

地址：ws://127.0.0.1:7100/v1/ws。消息体与 POST /v1/chat 相同：

    {"message":"你好","client_id":"ws-desktop"}

成功时发送与 ChatResponse 相同的 JSON。输入或处理异常时发送 {"error":"..."}；服务端不把这种错误文本伪装成正常 reply。

当前实现从 WebSocket 请求头 x-ling-token 取令牌；为兼容不能自定义头的客户端，也接受查询参数 ?token=...。认证失败在 accept 前以 4401 关闭。查询令牌可能进入代理日志，因此只允许本机开发使用；跨设备部署应使用能设置请求头的客户端或受控加密通道，并清理访问日志。

WebSocket 断线后，客户端回退到 HTTP health/state/events/chat 流程，按事件 ID 去重。它不是公共评论区、群聊或设备控制通道。

## 9. 最小契约测试

1. 五个固定 HTTP 端点无令牌均返回 401。
2. chat 的 {message,client_id} 请求成功，空白消息返回 422。
3. 关闭并重开服务后，memory/search 仍能查到上一段保存的事实。
4. events?limit=1 只返回一条；limit=0 或 201 返回 422。
5. state 是投影对象，health 含 schema_version=1 和 database=ok。
6. 使用扩展 WS 的客户端断线后，可以只靠固定 HTTP 端点恢复。
