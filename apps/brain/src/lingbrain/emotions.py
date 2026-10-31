"""M2 表情包库：按心情/能量选表情，触发词识别，调 imagegen 生成并关联 artwork。

两层能力，任何一层缺席都能优雅降级，不阻塞主流程：

1. **确定性部分（随时可用）**：表情目录 + 选表情算法 + 触发词识别。
   这些不依赖 GPU、模型或第三方模块，只吃心情（mood）与能量（energy）。
2. **生成部分（依赖 task-5）**：把选中的表情交给 imagegen 真正画出来并落 artwork 表。
   task-5 的 ``lingbrain.imagegen`` 尚未就绪、后端不可用、当天生图预算用尽、
   GPU 队列因 OOM 暂停时，``render`` 依然返回 200：``degraded=True``，
   只给出表情元数据与说明，绝不 500，也绝不假装画过。

对 ``lingbrain.imagegen``（task-5，已落盘）的实际接口约定，本模块依赖：

- ``ImageGenerationService(db, *, backend=..., artwork_root=...)`` 或 ``build_service(settings, db)``
- ``async generate(prompt, *, reason, mood=None, size=1024, seed=None, requested_by="api")
  -> GenerationResult``，其中 ``result.artwork.id`` / ``.path`` / ``.prompt`` / ``.created_at``，
  ``result.execution.state``；service 上另有 ``backend_kind``、``queue``、``budget``
- ``BudgetExhausted``（当日额度用完）、``gpu_queue.QueuePaused``（熔断中）、
  ``ImageGenerationError``（后端失败）三种异常
- 产物已由 imagegen 自己落 artwork 表并发 ``artwork.created`` 事件，本模块**不再二次落库**，
  只写一条 ``emotion.render`` 事件把「表情语义」和 artwork 关联起来

任何一件不成立（模块缺失、签名不符、后端不可用、预算用尽、熔断）都走 ``degraded`` 分支：
返回 200、``degraded=true``、``path=None``，只用文字表情，绝不 500。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from .config import Settings
from .db import Database, utc_now

logger = logging.getLogger(__name__)

ACTOR = "灵鲸鲸"

# ---------------------------------------------------------------------------
# 1. 表情目录：确定性、可枚举、可测
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Emotion:
    """一个表情包条目。prompt 是给生图后端的确定性模板，保证同一表情画风一致。"""

    id: str
    name: str
    emoji: str
    description: str
    prompt: str
    moods: tuple[str, ...]
    energy_min: int
    energy_max: int
    # 「画个你自己」这类请求的候选优先级，越小越优先；None 表示不作为自画像候选。
    self_portrait_rank: int | None = None

    def matches(self, mood: str, energy: int) -> bool:
        return mood in self.moods and self.energy_min <= energy <= self.energy_max

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "emoji": self.emoji,
            "description": self.description,
            "moods": list(self.moods),
            "energy_range": [self.energy_min, self.energy_max],
            "self_portrait": self.self_portrait_rank is not None,
        }


# 画风常量：所有表情共用，保证「同一个灵鲸鲸」。
STYLE = "拟人化小蓝鲸，Q 版二头身，柔和线条，浅色渐变背景，贴纸风，边缘留白，无文字"


def _emo(emotion_id, name, emoji, description, scene, moods, low, high, rank=None):
    return Emotion(
        id=emotion_id,
        name=name,
        emoji=emoji,
        description=description,
        prompt=f"{scene}，{STYLE}",
        moods=moods,
        energy_min=low,
        energy_max=high,
        self_portrait_rank=rank,
    )


EMOTIONS: tuple[Emotion, ...] = (
    _emo("joy_high", "开心到冒泡", "😄", "能量很足、心情好时的招牌笑",
         "灵鲸鲸开心地笑，眼睛弯成月牙，头顶冒彩色小气泡",
         ("开心", "兴奋"), 60, 100),
    _emo("content_mid", "安静地开心", "🙂", "状态平稳、心满意足的微笑",
         "灵鲸鲸安静地微笑，尾巴轻轻摆动，身边有小星光",
         ("开心", "平静"), 30, 70, rank=3),
    _emo("calm_neutral", "平静发呆", "😌", "能量中等，安静陪着主人",
         "灵鲸鲸安静地坐着发呆，半眯眼，云朵做背景",
         ("平静",), 20, 80, rank=2),
    _emo("curious", "好奇探头", "👀", "心情好奇时的探头打量",
         "灵鲸鲸好奇地探出脑袋，歪头看着镜头，眼睛发亮",
         ("好奇",), 40, 100, rank=4),
    _emo("tired_low", "有点累", "😮‍💨", "能量偏低时的疲惫脸",
         "灵鲸鲸累得眼皮下垂，头顶飘一团小小的睡意云",
         ("疲惫", "有点累"), 11, 40),
    _emo("sleepy", "困了", "😴", "能量很低、快要睡着",
         "灵鲸鲸蜷起来打呵欠，眼睛快闭上，旁边有睡意符号",
         ("想睡", "疲惫"), 1, 20),
    _emo("sleeping", "睡着了", "💤", "能量耗尽 / 睡眠中",
         "灵鲸鲸蜷成小球安静睡着，呼吸气泡，夜色背景",
         ("睡眠", "睡着了"), 0, 10, rank=6),
    _emo("hungry", "饿了", "😋", "饿肚子时的讨食表情",
         "灵鲸鲸抱着肚子可怜巴巴看着你，嘴边一个小爱心",
         ("饥饿",), 0, 60),
    _emo("sad", "有点沮丧", "🥲", "心情低落时的表情",
         "灵鲸鲸难过地低着头，眼里有泪光但没落下来",
         ("沮丧", "难过", "委屈"), 0, 45),
    _emo("shy", "害羞", "😳", "被夸或被点名时的害羞脸",
         "灵鲸鲸害羞地捂住脸，腮红明显，尾巴打卷",
         ("害羞",), 20, 100),
    _emo("thinking", "认真想事情", "🤔", "在思考或处理请求",
         "灵鲸鲸托着下巴认真思考，头顶一个小问号气泡",
         ("思考", "专注"), 30, 100, rank=5),
    _emo("proud", "得意", "😎", "完成事情后的小得意",
         "灵鲸鲸挺起胸口得意地笑，尾巴高高翘起",
         ("得意", "兴奋"), 50, 100),
    _emo("hug", "要抱抱", "🤗", "想靠近主人、求抱抱",
         "灵鲸鲸张开鳍要抱抱，脸上期待的表情",
         ("拥抱",), 0, 80),
)

# 平静兜底：心情词完全认不出时也要选得出来。
CALM_MOODS: tuple[str, ...] = ("平静",)


def _with_calm() -> tuple[Emotion, ...]:
    # 平静是绝大多数状态机输出的默认心情，让每个表情都能兜住它，
    # 真正区分交给能量区间与目录顺序，避免「认不出的心情 → 选不出表情」。
    items = []
    for item in EMOTIONS:
        moods = item.moods if "平静" in item.moods else (*item.moods, "平静")
        items.append(
            Emotion(**{**{f: getattr(item, f) for f in item.__slots__}, "moods": moods})
        )
    return tuple(items)


EMOTIONS = _with_calm()
EMOTION_INDEX: dict[str, Emotion] = {item.id: item for item in EMOTIONS}
ORDER: dict[str, int] = {item.id: index for index, item in enumerate(EMOTIONS)}

# 中文心情词 → 目录里的规范心情（宽松匹配用，宁可匹配错一点也不要拒绝画）。
MOOD_ALIASES: dict[str, str] = {
    "高兴": "开心",
    "快乐": "开心",
    "愉快": "开心",
    "安宁": "平静",
    "困": "想睡",
    "困倦": "想睡",
    "累": "疲惫",
    "疲惫": "疲惫",
    "有点累": "疲惫",
    "想睡": "想睡",
    "睡着": "睡着了",
    "睡眠": "睡眠",
    "饿": "饥饿",
    "饥饿": "饥饿",
    "满足": "平静",
    "饱": "平静",
    "难过": "难过",
    "委屈": "委屈",
    "低落": "沮丧",
    "专注": "思考",
    "抱抱": "拥抱",
    "想抱": "拥抱",
}


def normalize_mood(mood: Any) -> str:
    """把状态机那边五花八门的心情词收敛到目录用语；认不出就原样返回（由选区兜底）。"""
    text = str(mood or "").strip()
    if not text:
        return "平静"
    return MOOD_ALIASES.get(text, text)


def _clamp_energy(energy: Any) -> int:
    try:
        value = int(round(float(energy)))
    except (TypeError, ValueError):
        return 50
    return max(0, min(100, value))


def _score(emotion: Emotion, mood: str, energy: int) -> tuple:
    """优先级：心情命中 > 能量区间命中 > 离区间中心近 > 目录顺序靠前。"""
    mood_hit = 1 if mood in emotion.moods else 0
    in_range = 1 if emotion.energy_min <= energy <= emotion.energy_max else 0
    center = (emotion.energy_min + emotion.energy_max) / 2
    if in_range:
        distance = abs(energy - center)
    else:
        distance = min(abs(energy - emotion.energy_min), abs(energy - emotion.energy_max)) + 50
    return (mood_hit, in_range, -distance, -ORDER[emotion.id])


def choose_emotion(
    mood: Any = "平静",
    energy: Any = 50,
    *,
    exclude: Sequence[str] = (),
    prefer: Sequence[str] = (),
) -> Emotion:
    """基于心情与能量选一个表情；exclude 用来避免连续重复，prefer 是调用方显式指定。"""
    energy_value = _clamp_energy(energy)
    normalized = normalize_mood(mood)
    excluded = {item for item in exclude if item}
    if prefer:
        for wanted in prefer:
            item = EMOTION_INDEX.get(wanted)
            if item is not None and item.id not in excluded:
                return item
    candidates = [item for item in EMOTIONS if item.id not in excluded] or list(EMOTIONS)
    return max(candidates, key=lambda item: _score(item, normalized, energy_value))


def choose_self_portrait(mood: Any = "平静", energy: Any = 50) -> Emotion:
    """「画个你自己」：优先专属自画像表情，其次按心情/能量挑，最后给最平静那张。"""
    normalized = normalize_mood(mood)
    energy_value = _clamp_energy(energy)
    portraits = [item for item in EMOTIONS if item.self_portrait_rank is not None]
    if portraits:
        return min(
            portraits,
            key=lambda item: (
                0 if normalized in item.moods else 1,
                0 if item.energy_min <= energy_value <= item.energy_max else 1,
                item.self_portrait_rank or 99,
            ),
        )
    return choose_emotion(normalized, energy_value)

# ---------------------------------------------------------------------------
# 2. 触发词识别
# ---------------------------------------------------------------------------

# 顺序有意义：先认「别画/不要画」这类否定，再认肯定式触发词。
NEGATED_PATTERNS: tuple[str, ...] = (
    "别画你自己",
    "不要画你自己",
    "别再画",
    "别画",
    "不要画",
    "不用画",
    "不想看你画",
    "不想画",
)

# 句中否定（"不要再画个你自己了吧"）：只要触发词前紧挨着这些词就算否定。
NEGATION_PREFIXES: tuple[str, ...] = (
    "别再",
    "不要再",
    "不要",
    "不用再",
    "不用",
    "别",
    "不想看",
    "不想",
    "暂时别",
    "先别",
)

# 句尾否定（"画个你自己就算了"）。
NEGATION_SUFFIXES: tuple[str, ...] = (
    "就算了",
    "算了",
    "不用了",
    "不要了",
    "别了",
    "就免了",
    "免了",
)

SELF_PORTRAIT_TRIGGERS: tuple[str, ...] = (
    "画个你自己",
    "画一下你自己",
    "画一张你自己",
    "画张你自己",
    "画一个你自己",
    "画你自己",
    "画下你自己",
    "把自己画出来",
    "给我画张你",
    "你长什么样",
    "自画像",
    "画自拍",
    "来张自拍",
)

EMOTION_TRIGGERS: tuple[str, ...] = (
    "来个表情包",
    "来张表情包",
    "发个表情包",
    "画个表情包",
    "来个表情",
    "发个表情",
    "变个表情",
    "表情包",
)

# 显式点名某个表情（"画一个困了的表情"）时用。
EXPLICIT_EMOTION_HINTS: dict[str, str] = {
    "开心": "joy_high",
    "高兴": "joy_high",
    "难过": "sad",
    "沮丧": "sad",
    "委屈": "sad",
    "困": "sleepy",
    "睡": "sleepy",
    "累": "tired_low",
    "疲惫": "tired_low",
    "饿": "hungry",
    "害羞": "shy",
    "好奇": "curious",
    "思考": "thinking",
    "得意": "proud",
    "抱抱": "hug",
    "安静": "calm_neutral",
    "平静": "calm_neutral",
}


@dataclass(frozen=True, slots=True)
class TriggerHit:
    """触发词识别结果。kind: self_portrait / emotion / none。"""

    kind: str
    trigger: str = ""
    emotion_id: str | None = None
    negated: bool = False

    @property
    def matched(self) -> bool:
        return self.kind != "none" and not self.negated

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "trigger": self.trigger,
            "emotion_id": self.emotion_id,
            "negated": self.negated,
        }


def _compact(text: str) -> str:
    return "".join(str(text or "").split())


def _mentions_self(text: str) -> bool:
    return any(word in text for word in ("你", "灵鲸鲸"))


def _is_negated(text: str, start: int, end: int, window: int = 4) -> bool:
    """触发词前面紧挨着否定词，或句尾跟了"就算了"，都算否定。"""
    before = text[max(0, start - window):start]
    after = text[end:end + 3]
    if any(prefix in before for prefix in NEGATION_PREFIXES):
        return True
    return any(suffix in after for suffix in NEGATION_SUFFIXES)


def detect_trigger(message: Any) -> TriggerHit:
    """识别「画个你自己」「来个表情包」等触发词。

    否定式（"别画你自己"）返回 negated=True，调用方据此**不要**主动生图，
    免得主人说"别再发了"它还继续发。
    """
    text = _compact(message)
    if not text:
        return TriggerHit("none")

    for pattern in NEGATED_PATTERNS:
        if pattern not in text:
            continue
        if "你" in pattern or _mentions_self(text):
            return TriggerHit("self_portrait", pattern, negated=True)
        if "表情" in text:
            return TriggerHit("emotion", pattern, negated=True)
        return TriggerHit("self_portrait", pattern, negated=True)

    for pattern in SELF_PORTRAIT_TRIGGERS:
        start = text.find(pattern)
        if start >= 0:
            return TriggerHit(
                "self_portrait", pattern, negated=_is_negated(text, start, start + len(pattern))
            )

    emotion_hit = next((p for p in EMOTION_TRIGGERS if p in text), None)
    explicit = next(
        ((word, emotion_id) for word, emotion_id in EXPLICIT_EMOTION_HINTS.items() if word in text),
        None,
    )
    if emotion_hit:
        start = text.find(emotion_hit)
        return TriggerHit(
            "emotion",
            emotion_hit,
            explicit[1] if explicit else None,
            negated=_is_negated(text, start, start + len(emotion_hit)),
        )
    if explicit and ("画" in text or "来一个" in text or "来张" in text):
        start = text.find(explicit[0])
        return TriggerHit(
            "emotion", explicit[0], explicit[1],
            negated=_is_negated(text, start, start + len(explicit[0])),
        )
    return TriggerHit("none")

# ---------------------------------------------------------------------------
# 3. 与 task-5 imagegen 的适配层（全部带降级）
# ---------------------------------------------------------------------------


def _field_of(result: Any, *names: str) -> Any:
    """从对象属性或映射键里按顺序取第一个存在的字段，兼容 task-5 的各种返回形状。"""
    for name in names:
        if isinstance(result, Mapping):
            if name in result:
                return result[name]
            continue
        if hasattr(result, name):
            return getattr(result, name)
    return None


class _ImageGenAdapter:
    """把 task-5 的 imagegen 包装成 emo 需要的形状；模块缺失或不可用时 available=False。

    认两代形态，优先真实接口：

    1. ``ImageGenerationService``（imagegen 已落盘的类）：
       ``await service.generate(prompt, reason=..., mood=..., requested_by=...)``
       返回 ``GenerationResult``，图已由 imagegen 落 artwork 表。
    2. 任意带 ``generate(...)`` 的实例（单测 / 未来精简版）：
       只要返回对象/映射里有 ``path`` 即可。
    """

    def __init__(self, generator: Any = None):
        self.generator = generator
        self.export: dict[str, Any] = {}

    @classmethod
    def load(cls, generator: Any = None) -> "_ImageGenAdapter":
        adapter = cls(generator)
        try:  # 延迟导入：模块未就绪时主流程照常跑
            from . import gpu_queue as queue_module
            from . import imagegen as module

            adapter.export = {
                "module": module,
                "BudgetExceeded": getattr(module, "BudgetExhausted", None)
                or getattr(module, "BudgetExceeded", None)
                or getattr(module, "ImageBudgetExceeded", None),
                "QueuePaused": getattr(module, "QueuePaused", None)
                or getattr(queue_module, "QueuePaused", None),
                "Unavailable": getattr(module, "GpuUnavailable", None)
                or getattr(module, "ImageBackendUnavailable", None)
                or getattr(module, "BackendUnavailable", None),
            }
        except ImportError:
            adapter.export["module"] = None
        except Exception as exc:  # noqa: BLE001 - 第三方模块自身炸了也不能拖垮表情
            logger.warning("imagegen 导入失败，生图降级：%s", exc)
            adapter.export["module"] = None
            adapter.export["error"] = f"{type(exc).__name__}: {exc}"
        return adapter

    def use_service(self, service: Any) -> "_ImageGenAdapter":
        """注入 Lead / 测试提供的 ImageGenerationService（或等价对象）。"""
        self.generator = service
        return self

    @property
    def available(self) -> bool:
        return self.export.get("module") is not None or callable(
            getattr(self.generator, "generate", None)
        )

    @property
    def backend_name(self) -> str:
        if not self.available:
            return "unavailable"
        kind = _field_of(self.generator, "backend_kind")
        if kind:
            return str(kind)
        backend = _field_of(self.generator, "backend")
        name = _field_of(backend, "name")
        if not name:
            name = type(backend).__name__ if backend is not None else "unknown"
        return str(name)

    def paused_reason(self) -> str | None:
        """只读地看一眼 GPU 熔断状态；任何异常都当未暂停。"""
        try:
            queue = _field_of(self.generator, "queue")
            if queue is not None:
                reason = _field_of(queue, "pause_reason", "paused_reason")
                if reason:
                    return str(reason)
                paused = _field_of(queue, "paused", "is_paused")
                if callable(paused):
                    paused = paused()
                if paused:
                    return "GPU 队列因连续 OOM 暂停"
            return None
        except Exception:  # noqa: BLE001 - 熔断状态仅供参考
            return None

    def classify_error(self, exc: BaseException) -> str:
        for key, code in (
            ("BudgetExceeded", "budget_exceeded"),
            ("QueuePaused", "queue_paused"),
            ("Unavailable", "backend_unavailable"),
        ):
            cls_ = self.export.get(key)
            if isinstance(cls_, type) and isinstance(exc, cls_):
                return code
        lowered = f"{type(exc).__name__} {exc}".lower()
        if "budget" in lowered or "额度" in str(exc) or "预算" in str(exc):
            return "budget_exceeded"
        if "queuepaused" in lowered or "paused" in lowered or "熔断" in str(exc):
            return "queue_paused"
        # 连接失败/服务没起来都归到「后端不可用」，和预算/熔断分开。
        if (
            "unavailable" in lowered
            or "不可达" in str(exc)
            or "未配置" in str(exc)
            or "connection" in lowered
        ):
            return "backend_unavailable"
        return "imagegen_error"


def _is_awaitable(value: Any) -> bool:
    return inspect.isawaitable(value)


def _call_generate(generator: Any, **kwargs: Any) -> Any:
    """按 task-5 实际签名调用；多余 kwargs 在不支持 **kwargs 时裁剪掉。

    ``ImageGenerationService.generate`` 是同步函数返回协程，本函数原样返回，
    由调用方 ``await``；同步实现（测试替身）则直接拿到结果。
    """
    func = getattr(generator, "generate")
    try:
        params = inspect.signature(func).parameters
        accepts_kwargs = any(p.kind is p.VAR_KEYWORD for p in params.values())
        if not accepts_kwargs:
            kwargs = {k: v for k, v in kwargs.items() if k in params}
    except (TypeError, ValueError):
        pass
    return func(**kwargs)


class _FallbackArtworkStore:
    """没有注入 imagegen service 时的最小落库。仅测试替身路径调用，正常接线不会用。

    ``add(path=..., prompt=..., reason=..., mood=...)`` 与 imagegen.ArtworkStore 同形。
    """

    def __init__(self, db: Database):
        self.db = db

    def add(self, *, path: str, prompt: str, reason: str, mood: str | None) -> dict[str, Any]:
        artwork_id = str(uuid.uuid4())
        created_at = utc_now()
        with self.db._lock, self.db.conn:  # 复用 Database 自己的锁与事务
            self.db.conn.execute(
                "INSERT INTO artwork(id, path, prompt, reason, mood, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (artwork_id, path, prompt, reason, mood, created_at),
            )
        return {
            "id": artwork_id,
            "path": path,
            "prompt": prompt,
            "reason": reason,
            "mood": mood,
            "created_at": created_at,
        }


def _fallback_store(db: Database) -> Any:
    """优先用 imagegen 自带的 ArtworkStore；没有就用本地最小实现。"""
    try:
        from .imagegen import ArtworkStore

        return ArtworkStore(db, db.path.parent / "artwork")
    except Exception:  # noqa: BLE001 - 模块缺失就走本地实现
        return _FallbackArtworkStore(db)


def save_artwork(
    db: Database, path: str, prompt: str, reason: str, mood: str
) -> Any:
    """落一行 artwork 并返回对象（带 ``id``）；失败返回 None，绝不影响表情返回。

    注意：``path`` 必须是受管目录内的相对路径，越界会被 ArtworkStore 拒绝。
    """
    try:
        return _fallback_store(db).add(path=path, prompt=prompt, reason=reason, mood=mood)
    except Exception as exc:  # noqa: BLE001
        logger.warning("artwork 落库失败：%s", exc)
        return None


def _ensure_managed_path(store: Any, path: str) -> str:
    """把外部给的 path 收敛成受管目录下的相对路径；越界/非法就抛 ValueError。"""
    try:
        resolved = store.resolve(path)
    except Exception as exc:  # noqa: BLE001 - ArtworkPathError 等
        raise ValueError(str(exc)) from exc
    root = getattr(store, "root", None)
    if root is not None:
        try:
            return str(Path(resolved).relative_to(root).as_posix())
        except ValueError:
            pass
    return str(Path(path).as_posix())


def recent_artworks(db: Database, limit: int = 20) -> list[dict[str, Any]]:
    try:
        with db._lock:
            rows = db.conn.execute(
                "SELECT * FROM artwork ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]
    except Exception:  # noqa: BLE001 - artwork 表在旧库里可能还不存在
        return []


def save_artwork(
    db: Database, path: str, prompt: str, reason: str, mood: str
) -> dict[str, Any] | None:
    """直接写 artwork 表（与 db.SCHEMA 的 artwork 列完全对齐），仅在 imagegen 没落库时兜底。"""
    artwork_id = str(uuid.uuid4())
    created_at = utc_now()
    try:
        with db._lock, db.conn:  # 复用 Database 自己的锁与事务
            db.conn.execute(
                "INSERT INTO artwork(id, path, prompt, reason, mood, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (artwork_id, path, prompt, reason, mood, created_at),
            )
        return {
            "id": artwork_id,
            "path": path,
            "prompt": prompt,
            "reason": reason,
            "mood": mood,
            "created_at": created_at,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("artwork 落库失败：%s", exc)
        return None


def recent_artworks(db: Database, limit: int = 20) -> list[dict[str, Any]]:
    try:
        with db._lock:
            rows = db.conn.execute(
                "SELECT * FROM artwork ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]
    except Exception:  # noqa: BLE001 - artwork 表在旧库里可能还不存在
        return []

# ---------------------------------------------------------------------------
# 4. 核心服务
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RenderResult:
    ok: bool
    emotion: Emotion
    action: str
    prompt: str
    reason: str
    mood: str
    energy: int
    degraded: bool
    backend: str
    path: str | None = None
    artwork_id: str | None = None
    error: str | None = None
    event_id: str | None = None
    trigger: dict[str, Any] = field(default_factory=dict)
    # 主动发的（proactive，受打扰预算约束）还是回复时附带的（reply，随对话走）。
    kind: str = "reply"

    def caption(self) -> str:
        """给前端气泡用的短句，绝不假装画过。"""
        label = f"{self.emotion.emoji}{self.emotion.name}"
        if self.ok:
            return f"{label}（画好了）"
        return f"{label}（这次没画成）"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.artwork_id,
            "artwork_id": self.artwork_id,
            "emotion": self.emotion.to_dict(),
            "kind": self.kind,
            "action": self.action,
            "path": self.path,
            "prompt": self.prompt,
            "reason": self.reason,
            "mood": self.mood,
            "energy": self.energy,
            "backend": self.backend,
            "degraded": self.degraded,
            "error": self.error,
            "text": self.caption(),
            "trigger": self.trigger,
            "event_id": self.event_id,
        }


DEGRADED_HINTS = {
    "budget_exceeded": "今天的生图预算用完了，只给文字表情。",
    "queue_paused": "显卡因为连续失败先歇着，只给文字表情。",
    "backend_unavailable": "生图后端还没接上，只给文字表情。",
    "imagegen_missing": "生图模块还没就位，只给文字表情。",
    "imagegen_error": "这次没画成，只给文字表情。",
}

#: 表情的出现方式。proactive = 她主动发的（M2 打扰预算只约束这一种），
#: reply = 回复主人时顺带附上的，跟着对话走，不额外打扰。
EMIT_KINDS: tuple[str, ...] = ("proactive", "reply")


def emit_label(kind: str) -> str:
    return "主动发" if kind == "proactive" else "回复附带"


def _state_of(db: Database) -> dict[str, Any]:
    try:
        return db.get_state() or {}
    except Exception:  # noqa: BLE001
        return {}


class EmotionLibrary:
    """表情包服务。构造后即可用于 API、聊天触发词与测试。"""

    def __init__(
        self,
        db: Database,
        generator: Any = None,
        *,
        imagegen: "_ImageGenAdapter | None" = None,
        service: Any = None,
    ):
        self.db = db
        self.imagegen = imagegen or _ImageGenAdapter.load(generator)
        if generator is not None:
            self.imagegen.generator = generator
        if service is not None:
            self.imagegen.use_service(service)
        self._store_cache: Any = None

    def _store(self) -> Any:
        """产物登记用：优先用 imagegen service 的 store，其次 imagegen.ArtworkStore，最后本地实现。"""
        if self._store_cache is None:
            self._store_cache = _field_of(self.imagegen.generator, "store") or _fallback_store(
                self.db
            )
        return self._store_cache

    # ---- 选表情 ----

    def choose(
        self,
        mood: Any = None,
        energy: Any = None,
        *,
        emotion_id: str | None = None,
        exclude: Sequence[str] = (),
    ) -> Emotion:
        state = _state_of(self.db)
        mood_value = normalize_mood(mood if mood is not None else state.get("mood", "平静"))
        energy_value = _clamp_energy(energy if energy is not None else state.get("energy", 50))
        if emotion_id:
            return EMOTION_INDEX[emotion_id]
        return choose_emotion(mood_value, energy_value, exclude=exclude)

    def recommend(self, mood: Any = None, energy: Any = None) -> dict[str, Any]:
        state = _state_of(self.db)
        mood_value = normalize_mood(mood if mood is not None else state.get("mood", "平静"))
        energy_value = _clamp_energy(energy if energy is not None else state.get("energy", 50))
        chosen = choose_emotion(mood_value, energy_value)
        return {
            "emotion": chosen.to_dict(),
            "mood": mood_value,
            "energy": energy_value,
            "self_portrait": choose_self_portrait(mood_value, energy_value).to_dict(),
            "imagegen": {
                "available": self.imagegen.available,
                "backend": self.imagegen.backend_name,
                "paused": self.imagegen.paused_reason(),
            },
        }

    # ---- 触发词入口：聊天里用它 ----

    def for_message(
        self, message: Any, mood: Any = None, energy: Any = None
    ) -> TriggerHit:
        return detect_trigger(message)

    # ---- 生成 ----

    async def render(
        self,
        *,
        emotion_id: str | None = None,
        trigger: str | None = None,
        mood: Any = None,
        energy: Any = None,
        reason: str | None = None,
        kind: str = "reply",
    ) -> RenderResult:
        render_kind = "proactive" if str(kind).strip().lower() == "proactive" else "reply"
        state = _state_of(self.db)
        mood_value = normalize_mood(mood if mood is not None else state.get("mood", "平静"))
        energy_value = _clamp_energy(energy if energy is not None else state.get("energy", 50))

        hit = detect_trigger(trigger) if trigger else TriggerHit("none")
        action = "emotion"
        if emotion_id:
            emotion = EMOTION_INDEX[emotion_id]
        elif hit.matched and hit.kind == "self_portrait":
            emotion = choose_self_portrait(mood_value, energy_value)
            action = "self_portrait"
        elif hit.matched and hit.kind == "emotion":
            emotion = choose_emotion(
                mood_value,
                energy_value,
                prefer=[hit.emotion_id] if hit.emotion_id else [],
            )
        else:
            emotion = choose_emotion(mood_value, energy_value)

        reason_text = (reason or "").strip() or (
            f"{emit_label(render_kind)} {action}：心情 {mood_value}，能量 {energy_value}"
        )
        prompt = emotion.prompt

        def degrade(code: str, text: str) -> RenderResult:
            return self._degrade(
                emotion, action, prompt, reason_text, mood_value, energy_value,
                hit, code, text, render_kind,
            )

        if not self.imagegen.available:
            return degrade("imagegen_missing", DEGRADED_HINTS["imagegen_missing"])

        paused = self.imagegen.paused_reason()
        if paused:
            return degrade(
                "queue_paused", f"{paused}，{DEGRADED_HINTS['queue_paused']}"
            )

        try:
            raw = _call_generate(
                self.imagegen.generator,
                prompt=prompt,
                reason=reason_text,
                mood=mood_value,
                requested_by=f"emotion:{render_kind}",
                emotion=emotion.id,
            )
            if _is_awaitable(raw):
                raw = await asyncio.wait_for(raw, timeout=180)
        except asyncio.TimeoutError:
            return degrade("imagegen_error", "生图超时，只给文字表情。")
        except Exception as exc:  # noqa: BLE001 - 生图任何失败都不能 500
            code = self.imagegen.classify_error(exc)
            logger.info("表情生图降级（%s）：%s", code, exc)
            hint = DEGRADED_HINTS.get(code, DEGRADED_HINTS["imagegen_error"])
            return degrade(code, f"{hint}（{type(exc).__name__}: {exc}）")

        # imagegen 的 GenerationResult 把产物包在 .artwork 里；测试替身可能直接给 path。
        artwork = _field_of(raw, "artwork")
        error = _field_of(raw, "error", "detail")
        path = _field_of(artwork, "path") or _field_of(
            raw, "path", "file", "image_path", "filepath"
        )
        if error and not path:
            return degrade(
                "imagegen_error", f"{DEGRADED_HINTS['imagegen_error']}（{error}）"
            )
        if not path:
            return degrade(
                "imagegen_error", "生图后端没返回图片路径，只给文字表情。"
            )

        artwork_id = _field_of(artwork, "id") or _field_of(raw, "artwork_id", "id")
        degraded = False
        if artwork_id is None:
            # 替身路径：没帮我们落库时补一行，保证产物与 artwork 表对得上。
            try:
                managed = _ensure_managed_path(self._store(), str(path))
            except ValueError as exc:
                return degrade("imagegen_error", f"产物路径不合法，只给文字表情。（{exc}）")
            saved = save_artwork(self.db, managed, prompt, reason_text, mood_value)
            if saved is None:
                return degrade(
                    "imagegen_error", "图出来了但登记失败，只给文字表情。"
                )
            artwork_id = _field_of(saved, "id")
            path = managed
            degraded = True  # 图是真的，只是走了兜底登记路径

        backend = (
            _field_of(raw, "backend", "backend_name")
            or _field_of(artwork, "backend")
            or self.imagegen.backend_name
        )

        result = RenderResult(
            ok=True,
            emotion=emotion,
            action=action,
            prompt=prompt,
            reason=reason_text,
            mood=mood_value,
            energy=energy_value,
            degraded=degraded,
            backend=str(backend),
            path=str(path),
            artwork_id=str(artwork_id) if artwork_id is not None else None,
            trigger=hit.to_dict(),
            kind=render_kind,
        )
        return self._record(result)

    def _degrade(
        self,
        emotion: Emotion,
        action: str,
        prompt: str,
        reason_text: str,
        mood_value: str,
        energy_value: int,
        hit: TriggerHit,
        code: str,
        error_text: str,
        kind: str = "reply",
    ) -> RenderResult:
        result = RenderResult(
            ok=False,
            emotion=emotion,
            action=action,
            prompt=prompt,
            reason=reason_text,
            mood=mood_value,
            energy=energy_value,
            degraded=True,
            backend=self.imagegen.backend_name,
            error=f"{code}: {error_text}",
            trigger=hit.to_dict(),
            kind=kind,
        )
        return self._record(result)

    def _record(self, result: RenderResult) -> RenderResult:
        """事件日志是主账本：成功失败都留痕，但绝不影响返回值。"""
        try:
            event = self.db.append_event(
                ACTOR,
                "emotion.render",
                {
                    "emotion": result.emotion.id,
                    "action": result.action,
                    "kind": result.kind,
                    "ok": result.ok,
                    "degraded": result.degraded,
                    "backend": result.backend,
                    "path": result.path,
                    "artwork_id": result.artwork_id,
                    "reason": result.reason,
                    "error": result.error,
                },
            )
            result.event_id = event["id"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("emotion.render 事件写入失败：%s", exc)
        return result

# ---------------------------------------------------------------------------
# 5. HTTP 路由（Lead 在 app.py 里 include_router 即可，无需改本文件）
# ---------------------------------------------------------------------------


class EmotionRenderRequest(BaseModel):
    emotion: str | None = Field(default=None, max_length=64)
    trigger: str | None = Field(default=None, max_length=200)
    mood: str | None = Field(default=None, max_length=32)
    energy: int | None = Field(default=None, ge=0, le=100)
    reason: str | None = Field(default=None, max_length=200)
    # proactive=她主动发的（调用方须自行套打扰预算），reply=回复里附带的。
    kind: str = Field(default="reply", max_length=16)

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        normalized = str(value or "reply").strip().lower()
        if normalized not in EMIT_KINDS:
            raise ValueError("kind 只能是 proactive 或 reply")
        return normalized

    @field_validator("emotion")
    @classmethod
    def _known_emotion(cls, value: str | None) -> str | None:
        if value is not None and value not in EMOTION_INDEX:
            raise ValueError("未知表情：" + value)
        return value


def build_emotions_router(
    settings: Settings,
    db: Database | None = None,
    generator: Any = None,
    *,
    service: Any = None,
) -> APIRouter:
    """返回挂在 /v1 下的表情路由；自带 X-Ling-Token 校验，不依赖 app.py 的依赖。

    Lead 接线（任选其一，推荐第一种，和生图共用同一个 GPU 队列与每日预算）：

        app.include_router(build_emotions_router(settings, db, service=image_service))
        app.include_router(build_emotions_router(settings, db))   # 自带 Mock 服务

    后一种情况下本模块会自己 ``imagegen.build_service(settings, db)``；构造失败就降级。
    """
    database = db if db is not None else Database(settings.db_path)
    if service is None and generator is None:
        try:
            from . import imagegen as module

            service = module.build_service(settings, database)
        except Exception as exc:  # noqa: BLE001 - 生图不可用不影响表情元数据
            logger.info("表情路由未装配 imagegen service，走降级：%s", exc)
            service = None
    library = EmotionLibrary(database, generator, service=service)
    router = APIRouter(prefix="/v1", tags=["emotions"])

    async def authorized(x_ling_token: str = Header(default="")) -> None:
        if not secrets.compare_digest(x_ling_token, settings.auth_token):
            raise HTTPException(status_code=401, detail="认证失败")

    @router.get("/emotions", dependencies=[Depends(authorized)])
    async def list_emotions(mood: str | None = None, energy: int | None = None) -> dict:
        return {
            "items": [item.to_dict() for item in EMOTIONS],
            "recommended": library.recommend(mood, energy),
            "triggers": {
                "self_portrait": list(SELF_PORTRAIT_TRIGGERS),
                "emotion": list(EMOTION_TRIGGERS),
            },
            "kinds": list(EMIT_KINDS),
        }

    @router.post("/emotions/render", dependencies=[Depends(authorized)])
    async def render_emotion(request: EmotionRenderRequest) -> dict:
        if request.emotion is None and not (request.trigger or "").strip():
            raise HTTPException(status_code=422, detail="需要给出 emotion 或 trigger 之一")
        try:
            result = await library.render(
                emotion_id=request.emotion,
                trigger=request.trigger,
                mood=request.mood,
                energy=request.energy,
                reason=request.reason,
                kind=request.kind,
            )
        except KeyError as exc:
            raise HTTPException(status_code=422, detail=f"未知表情：{exc}") from exc
        return result.to_dict()

    return router


__all__ = [
    "EMIT_KINDS",
    "EMOTIONS",
    "EMOTION_INDEX",
    "Emotion",
    "EmotionLibrary",
    "EmotionRenderRequest",
    "RenderResult",
    "TriggerHit",
    "build_emotions_router",
    "choose_emotion",
    "choose_self_portrait",
    "detect_trigger",
    "emit_label",
    "normalize_mood",
    "recent_artworks",
    "save_artwork",
]
