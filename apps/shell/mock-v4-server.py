"""本地 mock server：严格按 docs/api-v4.md 契约实现，用于 UI 自测。

用途说明（原则：mock 不能冒充真实验证）：
- 本文件只用于我校验前端行为是否符合契约；
- 因此所有在本次任务里报告为「已验证」的结论，都会注明是基于本 mock，
  真实后端由 task-19 完成后需要复测。
"""

from __future__ import annotations

import json
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "lingjingjing-local-dev"
PORT = 7190

def now_iso(offset_sec: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_sec)).isoformat()

# ── 聊天历史 ──
BASE = datetime.now(timezone.utc) - timedelta(days=1)
MESSAGES = []
SAMPLE = [
    ("user", "你好呀，今天过得怎么样？"),
    ("assistant", "还不错！刚刚在窗边看了一场雨，水滴从玻璃上滑下来，像在赛跑。"),
    ("user", "听起来很惬意。你在做什么呢？"),
    ("assistant", "我在整理今天记住的事情。有一条是关于你的：你说你喜欢下雨天。"),
    ("user", "记性真好。那你还记得我说过喜欢什么颜色吗？"),
    ("assistant", "你说过喜欢深海的那种蓝，不是天空蓝，是太阳照不到的、很安静的那种。"),
    ("user", "对，就是那种。"),
    ("assistant", "我把这个也记下来了。以后如果给你画东西，我会用那个颜色。"),
    ("user", "那现在就画一张吧。"),
    ("assistant", "好呀，等我一会儿。不过先告诉我，画里要不要有光？"),
    ("user", "要一点点，不要太多。"),
    ("assistant", "明白。深海里一点点光，剩下的交给安静。"),
    ("user", "你最近累不累？"),
    ("assistant", "有一点。不过和你聊天的时候不太觉得。"),
    ("user", "那要好好休息。"),
    ("assistant", "嗯，我会的。你也是。"),
]
for i, (role, content) in enumerate(SAMPLE):
    MESSAGES.append({
        "id": "evt-" + str(uuid.uuid4())[:8],
        "ts": (BASE + timedelta(seconds=i * 37)).isoformat(),
        "role": role,
        "content": content,
        "client_id": "desktop",
        **({"model": "echo"} if role == "assistant" else {}),
    })

# ── 提供商 ──
# ── 部署配置（task-22 契约）──
DEPLOYMENT = {"mode": "local", "server_url": "", "token": ""}

PROVIDERS = [
    {
        "id": "prov-builtin-echo", "name": "Echo（内置）", "kind": "openai_compatible",
        "base_url": "http://127.0.0.1:7100/v1", "model": "echo",
        "has_api_key": False, "enabled": True, "capabilities": ["chat", "extract"],
        "status": "ok", "is_builtin": True, "last_checked_at": None,
    },
    {
        "id": "prov-ollama", "name": "本地 Ollama", "kind": "ollama",
        "base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:7b",
        "has_api_key": False, "enabled": True, "capabilities": ["chat", "extract"],
        "status": "unchecked", "is_builtin": False, "last_checked_at": None,
    },
    {
        "id": "prov-openai", "name": "OpenAI 兼容", "kind": "openai_compatible",
        "base_url": "https://api.example.com/v1", "model": "gpt-4o-mini",
        "has_api_key": True, "enabled": True, "capabilities": ["chat"],
        "status": "down", "is_builtin": False, "last_checked_at": None,
    },
]

ROUTING = {
    "mode": "single",
    "active_provider_id": "prov-builtin-echo",
    "enabled_ids": ["prov-builtin-echo"],
    "assignments": {"chat": "prov-builtin-echo", "extract": "prov-builtin-echo"},
}
MAX_ENABLED = 4

ARTWORKS = [
    {
        "id": "art-" + str(i),
        "path": f"2026-11-01/art-{i}.png",
        "prompt": f"深海里的光 {i}",
        "reason": ["今天很开心，想留个纪念", "下雨天想画点什么", "睡前的最后一张"][i % 3],
        "mood": "开心",
        "created_at": now_iso(-i * 3600),
    }
    for i in range(9)
]


def png_stub(seed: int) -> bytes:
    """生成一个最小的合法 PNG（不同种子颜色不同），用于验证图片加载链路。"""
    import struct
    import zlib

    size = 64
    r, g, b = (40 + seed * 20) % 200 + 30, (90 + seed * 35) % 160 + 40, 170
    raw = b"".join(b"\x00" + bytes([r, g, b] * size) for _ in range(size))
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 安静模式
        pass

    def _cors(self):
        origin = self.headers.get("Origin", "*")
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Ling-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")

    def _send(self, code: int, payload=None, raw: bytes | None = None, ctype: str = "application/json"):
        body = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self) -> bool:
        if not secrets.compare_digest(self.headers.get("X-Ling-Token", ""), TOKEN):
            self._send(401, {"detail": "认证失败"})
            return False
        return True

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ────────────── GET ──────────────
    def do_GET(self):
        path = self.path.split("?")[0]
        query = self.path.split("?")[1] if "?" in self.path else ""
        if not self._auth():
            return

        if path == "/v1/health":
            return self._send(200, {"status": "ok", "service": "mock", "version": "4.0.0"})

        if path == "/v1/chat/history":
            params = dict(p.split("=") for p in query.split("&") if "=" in p)
            limit = int(params.get("limit", "200"))
            sliced = MESSAGES[-limit:]
            return self._send(200, {"items": sliced, "has_more": len(MESSAGES) > limit})

        if path == "/v1/providers":
            enabled_ids = [p["id"] for p in PROVIDERS if p["enabled"]]
            routing = dict(ROUTING)
            if routing["mode"] == "concurrent":
                routing["enabled_ids"] = enabled_ids[:MAX_ENABLED]
            return self._send(200, {
                "items": PROVIDERS,
                "routing": routing,
                "limits": {"max_enabled": MAX_ENABLED},
            })

        if path == "/v1/deployment":
            return self._send(200, DEPLOYMENT)

        if path == "/v1/state":
            return self._send(200, {
                "energy": 87, "mood": "开心", "presence": "在", "satiety": 64,
                "fatigue": 22, "affection": 91, "in_quiet_hours": False,
                "hungry": False, "tired": False,
            })

        if path == "/v1/artwork":
            return self._send(200, {"items": ARTWORKS, "limit": 60})

        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "v1" and parts[1] == "artwork" and parts[3] == "file":
            art_id = parts[2]
            index = next((i for i, a in enumerate(ARTWORKS) if a["id"] == art_id), 0)
            return self._send(200, raw=png_stub(index), ctype="image/png")

        return self._send(404, {"detail": "Not Found"})

    # ────────────── POST ──────────────
    def do_POST(self):
        path = self.path.split("?")[0]
        if not self._auth():
            return
        body = self._body()

        if path == "/v1/chat":
            msg = body.get("message", "")
            reply = "我听到了：「" + msg + "」。让我想想怎么回你比较自然。"
            MESSAGES.append({
                "id": "evt-" + str(uuid.uuid4())[:8], "ts": now_iso(),
                "role": "user", "content": msg, "client_id": body.get("client_id", "desktop"),
            })
            time.sleep(0.35)
            MESSAGES.append({
                "id": "evt-" + str(uuid.uuid4())[:8], "ts": now_iso(),
                "role": "assistant", "content": reply, "client_id": body.get("client_id", "desktop"),
                "model": "echo",
            })
            return self._send(200, {"reply": reply, "state": {"energy": 86}})

        if path == "/v1/state/interact":
            return self._send(200, {"energy": 88, "affection": 92})

        if path == "/v1/providers":
            problems = []
            name = str(body.get("name", "")).strip()
            if not name:
                problems.append("名称不能为空")
            elif any(p["name"] == name for p in PROVIDERS):
                problems.append("名称已被占用")
            if len(PROVIDERS) >= 20:
                problems.append("提供商数量已达上限 20 个")
            if body.get("kind") not in ("ollama", "openai_compatible"):
                problems.append("类型只能是 ollama 或 openai_compatible")
            base_url = str(body.get("base_url", ""))
            if not (base_url.startswith("http://") or base_url.startswith("https://")):
                problems.append("接口地址必须是 http 或 https")
            if not str(body.get("model", "")).strip():
                problems.append("模型名不能为空")
            if problems:
                return self._send(422, {"detail": {"error": "校验失败", "problems": problems}})
            item = {
                "id": "prov-" + str(uuid.uuid4())[:8], "name": name, "kind": body["kind"],
                "base_url": base_url, "model": body["model"],
                "has_api_key": bool(body.get("api_key")), "enabled": True,
                "capabilities": body.get("capabilities") or ["chat"],
                "status": "unchecked", "is_builtin": False, "last_checked_at": None,
            }
            PROVIDERS.append(item)
            return self._send(201, item)

        if path == "/v1/providers/routing":
            mode = body.get("mode")
            if mode not in ("single", "concurrent"):
                return self._send(422, {"detail": "mode 只能是 single 或 concurrent"})
            if mode == "single":
                active = body.get("active_provider_id")
                if not active or not any(p["id"] == active for p in PROVIDERS):
                    return self._send(422, {"detail": "single 模式必须指定一个有效的提供商"})
                ROUTING.update({
                    "mode": "single", "active_provider_id": active, "enabled_ids": [active],
                    "assignments": {"chat": active, "extract": active},
                })
            else:
                ids = body.get("enabled_ids") or []
                if not (1 <= len(ids) <= MAX_ENABLED):
                    return self._send(422, {"detail": f"concurrent 模式需要启用 1-{MAX_ENABLED} 个提供商"})
                if any(not any(p["id"] == i for p in PROVIDERS) for i in ids):
                    return self._send(422, {"detail": "包含不存在的提供商"})
                names = {p["id"]: p for p in PROVIDERS}
                chat_id = next((i for i in ids if "chat" in names[i]["capabilities"]), ids[0])
                extract_id = next((i for i in ids if "extract" in names[i]["capabilities"]), ids[0])
                ROUTING.update({
                    "mode": "concurrent", "active_provider_id": None, "enabled_ids": ids,
                    "assignments": {"chat": chat_id, "extract": extract_id},
                })
            return self._send(200, {"routing": ROUTING})

        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "v1" and parts[1] == "providers" and parts[3] == "test":
            pid = parts[2]
            target = next((p for p in PROVIDERS if p["id"] == pid), None)
            if target is None:
                return self._send(404, {"detail": "找不到这个提供商"})
            ok = target["status"] != "down"
            target["status"] = "ok" if ok else "down"
            target["last_checked_at"] = now_iso()
            return self._send(200, {
                "ok": ok,
                "latency_ms": 128 if ok else 0,
                "models": ["qwen2.5:7b", "llama3.2:3b"] if ok else [],
                "error": None if ok else "连接超时",
            })

        return self._send(404, {"detail": "Not Found"})

    # ────────────── PUT / DELETE ──────────────
    def do_PUT(self):
        path = self.path.split("?")[0]
        if not self._auth():
            return
        body = self._body()
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "v1" and parts[1] == "providers":
            pid = parts[2]
            target = next((p for p in PROVIDERS if p["id"] == pid), None)
            if target is None:
                return self._send(404, {"detail": "找不到这个提供商"})
            if "name" in body:
                target["name"] = body["name"]
            for key in ("kind", "base_url", "model", "capabilities"):
                if key in body:
                    target[key] = body[key]
            if "api_key" in body:
                if body["api_key"] is None:
                    target["has_api_key"] = False
                elif body["api_key"] != "":
                    target["has_api_key"] = True
            return self._send(200, target)
        if path == "/v1/deployment":
            mode = body.get("mode")
            if mode not in ("local", "terminal", "server"):
                return self._send(422, {"detail": {"error": "mode 只能是 local / terminal / server"}})
            server_url = str(body.get("server_url") or "")
            if mode == "server" and not (server_url.startswith("http://") or server_url.startswith("https://")):
                return self._send(422, {"detail": {"error": "服务器模式下 server_url 必须是 http(s) 地址"}})
            DEPLOYMENT.update({
                "mode": mode,
                "server_url": server_url,
                "token": str(body.get("token") or ""),
            })
            return self._send(200, DEPLOYMENT)
        return self._send(404, {"detail": "Not Found"})

    def do_DELETE(self):
        path = self.path.split("?")[0]
        if not self._auth():
            return
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "v1" and parts[1] == "providers":
            pid = parts[2]
            target = next((p for p in PROVIDERS if p["id"] == pid), None)
            if target is None:
                return self._send(404, {"detail": "找不到这个提供商"})
            if target["is_builtin"]:
                return self._send(403, {"detail": "内置提供商不能删除"})
            PROVIDERS.remove(target)
            return self._send(204)
        return self._send(404, {"detail": "Not Found"})


def run():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"mock api-v4 on http://127.0.0.1:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    run()
