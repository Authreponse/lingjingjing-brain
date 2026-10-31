"""生图服务：后端抽象、每日预算与产物落库。

后端协议都走统一 GPU 队列（队列键 gpu:local:0）：

- MockBackend：无 GPU / 无 ComfyUI 也能验收。用 Pillow 画一张真实可解码的 PNG
  （按心情与提示词取色、画一道海面与鲸身弧线），产出不是占位文本，读图工具能打开。
- ComfyUIBackend：只做 HTTP 客户端。POST /prompt 提交工作流，轮询 /history/{id}，
  再从 /view 取图；不在本机调用 GPU、不启动 ComfyUI 服务。连接失败归一成
  GpuUnavailable，node_errors 里带 allocation 的返回归一成 GpuOutOfMemory。

每日预算默认 10 张（熔断规则参数 image_generation_per_day），可由构造参数或环境变量
LING_IMAGE_DAILY_BUDGET 调整；计数以 artwork.created_at 落在本地日为口径，产物落库
成功才计一张，失败与熔断拒绝不占额度。
"""

from __future__ import annotations

import asyncio
import base64
import colorsys
import hashlib
import io
import json
import logging
import os
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .db import Database, utc_now
from .gpu_queue import (
    GpuExecution,
    GpuExecutor,
    GpuJob,
    GpuOutOfMemory,
    GpuQueue,
    GpuQueueHook,
    GpuUnavailable,
    QueuePaused,
)

LOGGER = logging.getLogger("lingbrain.imagegen")

#: 固定画风前缀：每张图都带上它，保证自画像与表情包风格可比。
STYLE_PREFIX = (
    "灵鲸鲸固定画风：蓝鲸体型的软糯简笔插画，圆润线条，低饱和海蓝与乳白色，"
    "微光点缀，安静温柔，无文字水印。"
)

#: 生图模型标识，进队列与事件。
IMAGE_MODEL_ID = "sdxl-1.0-base"

#: v1 暂定每日生图预算；主人可通过配置修改。
DEFAULT_DAILY_BUDGET = 10

#: 单张出图超时（秒）。20 秒级为目标，8GB 上留足加载与卸载余量。
DEFAULT_TIMEOUT_SECONDS = 90.0

ALLOWED_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
ALLOWED_SIZES = frozenset({256, 512, 768, 1024})

SHANGHAI = "Asia/Shanghai"


class ImageGenerationError(RuntimeError):
    """生图失败（后端异常、返回非法内容等）。"""


class BudgetExhausted(RuntimeError):
    """当日生图额度已用完。"""


class ArtworkPathError(ValueError):
    """产物路径越界或扩展名不在白名单内。"""


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(SHANGHAI)
    except ZoneInfoNotFoundError:  # 精简 Windows 环境可能缺 tzdata：退回 UTC
        from datetime import timezone

        return timezone.utc  # type: ignore[return-value]


def local_day(now: datetime | None = None) -> str:
    moment = now or datetime.now(_tz())
    return moment.astimezone(_tz()).strftime("%Y-%m-%d")


def validate_size(size: int) -> int:
    if size not in ALLOWED_SIZES:
        raise ValueError(f"尺寸只接受 {sorted(ALLOWED_SIZES)}，收到 {size}")
    return size


def compose_prompt(prompt: str, *, style_prefix: str = STYLE_PREFIX) -> str:
    """固定画风前缀 + 主体描述，存进 artwork.prompt 的完整提示词。"""
    body = " ".join(prompt.split())
    if not body:
        raise ValueError("prompt 不能为空")
    return f"{style_prefix} {body}"


# ---------------------------------------------------------------- 后端协议
class ImageBackend(Protocol):
    name: str

    async def load(self) -> None: ...

    async def unload(self) -> None: ...

    async def generate(
        self, prompt: str, *, size: int, seed: int
    ) -> bytes: ...


def _png_bytes(image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


class MockBackend:
    """无 GPU 验收后端：用 Pillow 生成真实 PNG（可解码、可读图）。"""

    name = "mock"

    def __init__(self, *, fail_with: BaseException | None = None, fail_times: int = 0):
        self.loaded = False
        self.load_calls = 0
        self.unload_calls = 0
        self.generate_calls = 0
        self.last_prompt = ""
        self._fail_with = fail_with
        self._fail_times = fail_times
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        self.load_calls += 1
        self.loaded = True

    async def unload(self) -> None:
        self.unload_calls += 1
        self.loaded = False

    def _arm_failure(self) -> None:
        """按次数注入失败，模拟 OOM / 后端异常，用于熔断测试。"""
        if self._fail_with is None or self._fail_times <= 0:
            return
        self._fail_times -= 1
        exc = self._fail_with
        if self._fail_times <= 0:
            self._fail_with = None
        raise exc

    async def generate(self, prompt: str, *, size: int, seed: int) -> bytes:
        # 模拟显存互斥：同一时刻只允许一个任务出图（真实后端也只有一个模型驻留）。
        if self._lock.locked():
            raise AssertionError("MockBackend 被并发调用：GPU 队列互斥失效")
        async with self._lock:
            if not self.loaded:
                raise ImageGenerationError("模型尚未加载")
            self.generate_calls += 1
            self.last_prompt = prompt
            self._arm_failure()
            await asyncio.sleep(0)  # 让出事件循环，暴露真实的并发问题
            return await asyncio.to_thread(self._render, prompt, size, seed)

    def _render(self, prompt: str, size: int, seed: int) -> bytes:
        from PIL import Image, ImageDraw

        rng = random.Random(seed)
        base_h, base_s = 0.55 + 0.12 * warmth_factor(prompt), 0.55
        image = Image.new("RGB", (size, size), (0, 0, 0))
        draw = ImageDraw.Draw(image)
        # 天空：自上而下由乳白过渡到海蓝
        for y in range(size):
            t = y / max(1, size - 1)
            r, g, b = colorsys.hsv_to_rgb(base_h, base_s * (0.25 + 0.6 * t), 0.97 - 0.28 * t)
            draw.line([(0, y), (size, y)], fill=(int(r * 255), int(g * 255), int(b * 255)))
        # 鲸身弧线与海面微光
        body_bounds = (
            size * 0.16,
            size * 0.40,
            size * 0.84,
            size * 0.80,
        )
        draw.ellipse(body_bounds, fill=(24, 58, 92), outline=(9, 28, 48), width=max(2, size // 160))
        eye_r = max(3, size // 90)
        draw.ellipse(
            (size * 0.35 - eye_r, size * 0.55 - eye_r, size * 0.35 + eye_r, size * 0.55 + eye_r),
            fill=(245, 248, 252),
        )
        for _ in range(max(6, size // 40)):
            x = rng.uniform(0, size)
            y = rng.uniform(size * 0.82, size * 0.99)
            w = rng.uniform(size * 0.01, size * 0.05)
            draw.line([(x, y), (x + w, y)], fill=(226, 238, 246), width=max(1, size // 512))
        # 右下角微光
        glow = size * 0.06
        draw.ellipse(
            (size - glow * 2.2, glow * 0.4, size - glow * 0.4, glow * 2.2),
            fill=(238, 246, 252),
        )
        return _png_bytes(image)


def warmth_factor(prompt: str) -> float:
    """提示词里的心情词决定色相偏冷还是偏暖；相同提示词永远同一组配色。"""
    if any(word in prompt for word in ("困", "低", "累", "灰")):
        return 0.0
    if "开心" in prompt:
        return 0.35
    return 0.15


#: 默认 ComfyUI 工作流模板（SDXL 基础 txt2img）。节点 id 固定，参数按占位符替换。
DEFAULT_WORKFLOW: dict[str, Any] = {
    "4": {
        "class_type": "CheckpointLoaderSimple",
        "inputs": {"ckpt_name": "sd_xl_base_1.0.safetensors"},
    },
    "5": {
        "class_type": "EmptyLatentImage",
        "inputs": {"width": "{size}", "height": "{size}", "batch_size": 1},
    },
    "6": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "{positive}", "clip": ["4", 1]},
    },
    "7": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "low quality, watermark, text, extra limbs", "clip": ["4", 1]},
    },
    "3": {
        "class_type": "KSampler",
        "inputs": {
            "seed": "{seed}",
            "steps": 28,
            "cfg": 6.5,
            "sampler_name": "dpmpp_2m",
            "scheduler": "karras",
            "denoise": 1.0,
            "model": ["4", 0],
            "positive": ["6", 0],
            "negative": ["7", 0],
            "latent_image": ["5", 0],
        },
    },
    "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
    "9": {
        "class_type": "SaveImage",
        "inputs": {"filename_prefix": "lingjingjing", "images": ["8", 0]},
    },
}


def _fill_workflow(template: Any, values: dict[str, Any]) -> Any:
    if isinstance(template, dict):
        return {key: _fill_workflow(value, values) for key, value in template.items()}
    if isinstance(template, list):
        return [_fill_workflow(value, values) for value in template]
    if isinstance(template, str) and template.startswith("{") and template.endswith("}"):
        key = template[1:-1]
        if key in values:
            return values[key]
    return template


class ComfyUIBackend:
    """ComfyUI HTTP 客户端。只发 HTTP 请求，不在本机起服务、不直接碰显存。"""

    name = "comfyui"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8188",
        *,
        workflow: dict[str, Any] | None = None,
        poll_seconds: float = 0.5,
        timeout_seconds: float = 30.0,
        transfer: Callable[[], Awaitable["httpx.AsyncClient"]] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.workflow = workflow or DEFAULT_WORKFLOW
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        self._client: httpx.AsyncClient | None = None
        self._client_id = f"ling-{uuid.uuid4().hex[:8]}"

    # -- 连接生命周期 -------------------------------------------------------
    async def load(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
        try:
            response = await self._client.get(f"{self.base_url}/system_stats")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            await self.unload()
            raise GpuUnavailable(f"ComfyUI 不可达（{self.base_url}）：{exc}") from exc

    async def unload(self) -> None:
        # ComfyUI 自管模型常驻策略；客户端侧只关闭连接，不发送卸载指令。
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- 生成 ---------------------------------------------------------------
    def build_prompt(self, prompt: str, *, size: int, seed: int) -> dict[str, Any]:
        return _fill_workflow(
            self.workflow, {"positive": prompt, "size": size, "seed": seed}
        )

    async def generate(self, prompt: str, *, size: int, seed: int) -> bytes:
        if self._client is None:
            raise ImageGenerationError("ComfyUI 客户端尚未连接")
        client = self._client
        workflow = self.build_prompt(prompt, size=size, seed=seed)
        try:
            response = await client.post(
                f"{self.base_url}/prompt",
                json={"prompt": workflow, "client_id": self._client_id},
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            raise GpuUnavailable(f"ComfyUI 提交失败：{exc}") from exc
        if "error" in data:
            error = data.get("error") or {}
            node_errors = data.get("node_errors") or {}
            blob = json.dumps(node_errors, ensure_ascii=False).lower()
            if "allocation" in blob or "out of memory" in blob or "cuda" in blob:
                raise GpuOutOfMemory("ComfyUI 报告显存分配失败")
            raise ImageGenerationError(f"ComfyUI 拒绝工作流：{error}")
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            raise ImageGenerationError("ComfyUI 未返回 prompt_id")

        deadline = time.monotonic() + self.timeout_seconds
        history: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            try:
                polled = await client.get(f"{self.base_url}/history/{prompt_id}")
                polled.raise_for_status()
                history = polled.json().get(prompt_id)
            except httpx.HTTPError as exc:
                raise GpuUnavailable(f"ComfyUI 轮询失败：{exc}") from exc
            if history:
                break
            await asyncio.sleep(self.poll_seconds)
        if not history:
            raise ImageGenerationError(f"ComfyUI 超时未产出结果（prompt_id={prompt_id}）")
        status = history.get("status") or {}
        if status.get("status_str") == "error":
            messages = json.dumps(status.get("messages", []), ensure_ascii=False).lower()
            if "allocation" in messages or "out of memory" in messages:
                raise GpuOutOfMemory("ComfyUI 执行时显存不足")
            raise ImageGenerationError("ComfyUI 执行工作流失败")
        images = [
            item
            for output in (history.get("outputs") or {}).values()
            for item in output.get("images", [])
        ]
        if not images:
            raise ImageGenerationError("ComfyUI 返回结果中没有图片")
        first = images[0]
        try:
            viewed = await client.get(
                f"{self.base_url}/view",
                params={
                    "filename": first["filename"],
                    "subfolder": first.get("subfolder", ""),
                    "type": first.get("type", "output"),
                },
            )
            viewed.raise_for_status()
        except httpx.HTTPError as exc:
            raise GpuUnavailable(f"ComfyUI 取图失败：{exc}") from exc
        return viewed.content


def detect_backend_kind(url: str | None) -> str:
    """本机没有 ComfyUI 时默认走 MockBackend；配置了地址才走 HTTP 客户端。"""
    return "comfyui" if url and url.strip() else "mock"


# ------------------------------------------------------------------ 产物
@dataclass(slots=True)
class Artwork:
    id: str
    path: str
    prompt: str
    reason: str
    mood: str | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "prompt": self.prompt,
            "reason": self.reason,
            "mood": self.mood,
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class GenerationResult:
    artwork: Artwork
    execution: GpuExecution
    budget: dict[str, Any] = field(default_factory=dict)
    seed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "artwork": self.artwork.to_dict(),
            "queue": {
                "state": self.execution.state,
                "task_id": self.execution.job.task_id,
                "history": self.execution.job.history,
            },
            "budget": self.budget,
            "seed": self.seed,
        }


def _row_to_artwork(row) -> Artwork:
    return Artwork(
        id=row["id"],
        path=row["path"],
        prompt=row["prompt"],
        reason=row["reason"],
        mood=row["mood"],
        created_at=row["created_at"],
    )


class ArtworkStore:
    """artwork 表的读写。写只接受受管目录内的相对路径与白名单扩展名。"""

    def __init__(self, db: Database, root: Path):
        self.db = db
        self.root = Path(root).resolve()

    def resolve(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ArtworkPathError(f"产物路径越界：{relative_path}")
        if candidate.suffix.lower() not in ALLOWED_EXTENSIONS:
            raise ArtworkPathError(f"不接受的扩展名：{candidate.suffix}")
        return candidate

    def add(
        self, *, path: str, prompt: str, reason: str, mood: str | None
    ) -> Artwork:
        self.resolve(path)  # 先校验再落库，绝不写入越界路径
        artwork_id = str(uuid.uuid4())
        created_at = utc_now()
        with self.db._lock, self.db.conn:
            self.db.conn.execute(
                "INSERT INTO artwork(id, path, prompt, reason, mood, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (artwork_id, path.replace(os.sep, "/"), prompt, reason, mood, created_at),
            )
        return Artwork(artwork_id, path.replace(os.sep, "/"), prompt, reason, mood, created_at)

    def list(
        self,
        limit: int = 50,
        *,
        mood: str | None = None,
        day: str | None = None,
    ) -> list[Artwork]:
        sql = "SELECT * FROM artwork"
        params: list[Any] = []
        conditions = []
        if mood:
            conditions.append("mood = ?")
            params.append(mood)
        if day:
            conditions.append("substr(created_at, 1, 10) = ?")
            params.append(day)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        with self.db._lock:
            rows = self.db.conn.execute(sql, params).fetchall()
        return [_row_to_artwork(row) for row in rows]

    def get(self, artwork_id: str) -> Artwork | None:
        with self.db._lock:
            row = self.db.conn.execute(
                "SELECT * FROM artwork WHERE id = ?", (artwork_id,)
            ).fetchone()
        return _row_to_artwork(row) if row else None

    def count_between(self, start_iso: str, end_iso: str) -> int:
        """统计 created_at 落在 [start, end) 的行数。

        created_at 是带 +00:00 偏移的 ISO 串，而日界按本地时区算；直接做字符串
        比较会把偏移不同的写法比错，所以统一先转成 UTC 毫秒再比。
        """
        with self.db._lock:
            row = self.db.conn.execute(
                """
                SELECT COUNT(*) AS n FROM artwork
                WHERE CAST(strftime('%s', created_at) AS INTEGER) * 1000
                          + CAST(substr(created_at, 21, 3) AS INTEGER) >= ?
                  AND CAST(strftime('%s', created_at) AS INTEGER) * 1000
                          + CAST(substr(created_at, 21, 3) AS INTEGER) < ?
                """,
                (_epoch_ms(start_iso), _epoch_ms(end_iso)),
            ).fetchone()
        return int(row["n"])

    def find_by_reason(self, reason: str) -> list[Artwork]:
        with self.db._lock:
            rows = self.db.conn.execute(
                "SELECT * FROM artwork WHERE reason = ? ORDER BY created_at DESC, rowid DESC",
                (reason,),
            ).fetchall()
        return [_row_to_artwork(row) for row in rows]


def _epoch_ms(iso_ts: str) -> int:
    return int(datetime.fromisoformat(iso_ts).timestamp() * 1000)


def _day_bounds(day: str | None = None) -> tuple[str, str]:
    """本地日 -> [start_utc_iso, end_utc_iso)，与 artwork.created_at 同为 UTC ISO。"""
    from datetime import timedelta, timezone

    tz = _tz()
    if day is None:
        target = datetime.now(tz)
    else:
        parsed = datetime.strptime(day, "%Y-%m-%d")  # strptime 默认 1900 年，必须显式补全
        target = datetime(parsed.year, parsed.month, parsed.day, tzinfo=tz)
    start = target.astimezone(timezone.utc)
    end = (target + timedelta(days=1)).astimezone(timezone.utc)
    fmt = lambda moment: moment.isoformat(timespec="milliseconds")
    return fmt(start), fmt(end)


class DailyBudget:
    """每日生图预算。默认 10 张，可通过环境变量或构造参数覆盖。"""

    def __init__(self, store: ArtworkStore, limit: int = DEFAULT_DAILY_BUDGET):
        if limit < 0:
            raise ValueError("预算不能为负数")
        self.store = store
        self.limit = limit

    def snapshot(self, day: str | None = None) -> dict[str, Any]:
        target = day or local_day()
        start, end = _day_bounds(target)
        used = self.store.count_between(start, end)
        return {
            "day": target,
            "limit": self.limit,
            "used": used,
            "remaining": max(0, self.limit - used),
        }

    def check(self, day: str | None = None) -> dict[str, Any]:
        snapshot = self.snapshot(day)
        if snapshot["used"] >= self.limit:
            raise BudgetExhausted(
                f"{snapshot['day']} 的生图额度已用完（{snapshot['used']}/{self.limit}）"
            )
        return snapshot


class MockStorage:
    """测试替身：内存里记录落库调用，不碰 SQLite。"""

    def __init__(self):
        self.items: list[Artwork] = []
        self.fail_next = False

    def add(self, *, path: str, prompt: str, reason: str, mood: str | None) -> Artwork:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("模拟落库失败")
        artwork = Artwork(str(uuid.uuid4()), path, prompt, reason, mood, utc_now())
        self.items.append(artwork)
        return artwork

    def count_between(self, start_iso: str, end_iso: str) -> int:
        return sum(1 for item in self.items if start_iso <= item.created_at < end_iso)


# ------------------------------------------------------------------ 服务
class ImageGenerationService:
    def __init__(
        self,
        db: Database,
        *,
        backend: ImageBackend | None = None,
        backend_kind: str | None = None,
        comfyui_url: str | None = None,
        artwork_root: Path | None = None,
        daily_budget: int | None = None,
        queue: GpuQueue | None = None,
        executor: GpuExecutor | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.db = db
        self.artwork_root = Path(
            artwork_root or (db.path.parent / "artwork")
        ).resolve()
        self.store = ArtworkStore(db, self.artwork_root)
        limit = daily_budget
        if limit is None:
            limit = int(os.getenv("LING_IMAGE_DAILY_BUDGET", str(DEFAULT_DAILY_BUDGET)))
        self.budget = DailyBudget(self.store, limit)
        self.timeout_seconds = timeout_seconds

        self.backend_kind = backend_kind or (
            backend.name if backend is not None else detect_backend_kind(comfyui_url)
        )
        if backend is None:
            if self.backend_kind == "comfyui":
                backend = ComfyUIBackend(comfyui_url or "http://127.0.0.1:8188")
            elif self.backend_kind == "mock":
                backend = MockBackend()
            else:
                raise ValueError(f"未知生图后端：{self.backend_kind}")
        self.backend = backend

        if queue is None:
            hook = GpuQueueHook(db)
            queue = GpuQueue(notifier=hook, on_status=hook.on_status)
        self.queue = queue

        async def unload(_job: GpuJob) -> None:
            # 用完就卸载：8GB 上不允许生图模型常驻，与对话/VLM 抢显存。
            await self.backend.unload()

        self.executor = executor or GpuExecutor(queue, unload=unload)

    @property
    def queue_key(self) -> str:
        return self.queue.queue_key

    def status(self) -> dict[str, Any]:
        return {
            "backend": self.backend_kind,
            "budget": self.budget.snapshot(),
            "queue": self.queue.status(),
            "artwork_root": str(self.artwork_root),
        }

    async def generate(
        self,
        prompt: str,
        *,
        reason: str,
        mood: str | None = None,
        size: int = 1024,
        seed: int | None = None,
        requested_by: str = "api",
        timeout_seconds: float | None = None,
    ) -> GenerationResult:
        validate_size(size)
        full_prompt = compose_prompt(prompt)
        reason = reason.strip()
        if not reason:
            raise ValueError("reason 不能为空：每张图都要说清为什么画")
        if self.queue.paused:
            pause_reason = self.queue.status()["pause_reason"]
            await self._count_rejection("emergent", pause_reason)
            raise QueuePaused(pause_reason or "GPU 队列已暂停")
        budget_before = self.budget.check()
        seed = seed if seed is not None else random.SystemRandom().randint(1, 2**31 - 1)

        async def load(job: GpuJob) -> None:
            job.payload["size"] = size
            await self.backend.load()

        async def run(job: GpuJob) -> bytes:
            await self._write_job(job)
            return await self.backend.generate(full_prompt, size=size, seed=seed)

        execution = await self.executor.execute(
            "image",
            IMAGE_MODEL_ID,
            load,
            run,
            timeout_seconds=timeout_seconds or self.timeout_seconds,
            requested_by=requested_by,
            payload={"reason": reason, "mood": mood, "size": size, "seed": seed},
        )
        if not execution.ok:
            await self._count_rejection(execution.state, execution.error)
            raise ImageGenerationError(execution.error or "生图失败")

        data: bytes = execution.result
        if not data or data[:8] != b"\x89PNG\r\n\x1a\n":
            raise ImageGenerationError("后端返回的不是 PNG 图片")
        relative = Path(local_day()) / f"{execution.job.task_id}.png"
        target = self.store.resolve(relative.as_posix())
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        try:
            artwork = self.store.add(
                path=relative.as_posix(),
                prompt=full_prompt,
                reason=reason,
                mood=mood,
            )
        except Exception:
            # 落库失败就撤掉半成品，绝不留下无记录的孤儿文件。
            target.unlink(missing_ok=True)
            raise
        budget_after = self.budget.snapshot()
        self.db.append_event(
            "灵鲸鲸",
            "artwork.created",
            {
                "artwork_id": artwork.id,
                "path": artwork.path,
                "prompt": artwork.prompt,
                "reason": artwork.reason,
                "mood": artwork.mood,
                "task_id": execution.job.task_id,
                "model_id": IMAGE_MODEL_ID,
                "backend": self.backend_kind,
                "seed": seed,
                "size": size,
                "budget_day": budget_before["day"],
                "budget_used": budget_after["used"],
                "budget_limit": budget_after["limit"],
            },
        )
        return GenerationResult(
            artwork=artwork,
            execution=execution,
            budget=budget_after,
            seed=seed,
        )

    async def _write_job(self, job: GpuJob) -> None:
        event = self.db.append_event(
            "system",
            "image.job",
            {
                "task_id": job.task_id,
                "state": "running",
                "model_id": job.model_id,
                "reason": job.payload.get("reason"),
                "requested_by": job.requested_by,
            },
        )
        job.payload["job_event_id"] = event["id"]

    async def _count_rejection(self, state: str, error: str | None) -> None:
        """没出图的请求也要留痕，但绝不占每日额度。"""
        self.db.append_event(
            "system",
            "image.rejected",
            {"state": state, "error": error, "backend": self.backend_kind},
        )

    def list_artwork(
        self, limit: int = 50, *, mood: str | None = None, day: str | None = None
    ) -> list[Artwork]:
        return self.store.list(limit, mood=mood, day=day)


def build_service(settings, db: Database, **overrides) -> ImageGenerationService:
    """从 Settings 组装服务；comfyui_url 为空即自动选 MockBackend。"""
    options: dict[str, Any] = {
        "comfyui_url": getattr(settings, "comfyui_url", None),
        "backend_kind": getattr(settings, "image_backend", None) or None,
        "artwork_root": Path(getattr(settings, "data_dir", Path("."))) / "artwork",
        "daily_budget": getattr(settings, "image_daily_budget", None),
        "timeout_seconds": getattr(settings, "image_timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
    }
    options.update(overrides)
    options = {key: value for key, value in options.items() if value is not None}
    return ImageGenerationService(db, **options)
