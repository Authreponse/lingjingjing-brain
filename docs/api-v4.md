# V4 API 契约（前后端并行开发的唯一依据）

> 认证统一为请求头 `X-Ling-Token`。所有响应 UTF-8 JSON。时间一律 ISO 8601 带时区。

## 1. 聊天历史（新增）

### GET /v1/chat/history?limit=200&before=<iso可选>
返回按时间正序的消息（不是倒序），用于前端重建会话：

```json
{
  "items": [
    {"id": "evt-uuid", "ts": "2026-11-01T10:00:00+00:00", "role": "user", "content": "你好", "client_id": "desktop"},
    {"id": "evt-uuid", "ts": "2026-11-01T10:00:02+00:00", "role": "assistant", "content": "我在。", "client_id": "desktop", "model": "echo"}
  ],
  "has_more": false
}
```

- role 只有 "user" / "assistant"（system 事件不返回）。
- 前端负责把「一条 user + 紧随其后的 assistant」分成一组（对话轮次）；后端不预分组。
- limit 1–500 默认 200；before 传某条消息的 ts 可向前翻页。

## 2. 模型提供商注册表（新增）

### GET /v1/providers
```json
{
  "items": [
    {
      "id": "prov-uuid",
      "name": "本地 Ollama",
      "kind": "ollama | openai_compatible",
      "base_url": "http://127.0.0.1:11434/v1",
      "model": "qwen2.5:7b",
      "has_api_key": false,
      "enabled": true,
      "capabilities": ["chat", "extract"],
      "status": "ok | down | unknown | unchecked",
      "is_builtin": true,
      "last_checked_at": null
    }
  ],
  "routing": {
    "mode": "single | concurrent",
    "active_provider_id": "prov-uuid 或 null",
    "enabled_ids": ["..."],
    "assignments": {"chat": "prov-uuid", "extract": "prov-uuid", "image": "prov-uuid", "tts": "prov-uuid"}
  },
  "limits": {"max_enabled": 4}
}
```

### POST /v1/providers  （创建自定义提供商）
请求体：{"name": str(1-40), "kind": "openai_compatible"|"ollama", "base_url": str, "model": str, "api_key": str可选, "capabilities": ["chat","extract"]可选，默认["chat"]}
- 数量上限 20 个；名称全局唯一；base_url 必须是 http(s)，禁止指向元数据地址（169.254.、localhost 之外内网段仅警告）。
- api_key 只写不读：GET 只返回 has_api_key。

### PUT /v1/providers/{id}  （更新，字段同上全部可选；api_key 传空字符串=不修改，传 null=清除）
### DELETE /v1/providers/{id}  （内置的 echo 提供商不可删，返回 403）
### POST /v1/providers/{id}/test  （连通性测试：GET /models 或 /api/tags，超时 8s）
返回 {"ok": bool, "latency_ms": int, "models": [...], "error": str|null}

### POST /v1/providers/routing
请求体：{"mode": "single"|"concurrent", "active_provider_id": "..."(single 必填), "enabled_ids": [...](concurrent 必填, 1-4 个)}
- single 模式：只允许一个提供商参与全部任务。
- concurrent 模式：最多 4 个启用；后端按任务类型智能分配（chat→文本性能最好/最快响应，extract→最快便宜的，image/tts→有对应能力的）。
- 校验失败 422 带中文 detail。

## 3. 错误约定（与既有一致）
401 认证失败；404 不存在；422 校验失败（detail 可为字符串或 {"error","problems"}）；503 上游不可用。

## 4. 客户端启动握手（launcher 与前端共用）
- 后端启动后把实际端口与令牌写入 `data/runtime.json`：{"port": 7100, "token": "...", "pid": 1234, "started_at": "..."}
- launcher 先静默拉起后端（python -m lingbrain），轮询 runtime.json + /v1/health 直到 200，再拉起前端并注入 LING_BRAIN_URL / LING_BRAIN_TOKEN（env 或 query）。
- 前端读取顺序：URL query > localStorage > 默认(127.0.0.1:7100 / 内置开发令牌)。
