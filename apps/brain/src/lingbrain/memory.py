"""记忆抽取：规则快速路径 + 模型结构化抽取，两条路都留下可核来源。

设计要点：

1. **规则提取保留**：extract_memories 原样不动，作为快速路径与模型不可用时的兜底。
2. **模型抽取**：用已配置的 ChatModel 做一次结构化抽取，要求模型只输出 JSON 数组，
   每项 {kind, who, content, weight}，kind 只能取 event / preference / profile / relation。
3. **降级不隐藏**：模型不可用、返回不是合法 JSON、或模型给出的都不可核时，
   结果的 source 标成 rule-fallback，并附带 reason 说明为什么降级。
   这个 source 会原样写进 memory 表，之后能查到。
4. **可追溯**：每个 MemoryCandidate 都带 source_event_id（对应 event 表里那条
   user.message）；模型给出的内容必须在原文里有依据，否则丢弃。
5. **不把疑问句/引用写成事实**：继承并扩展 _is_fact_like：
   疑问词、问号、引号、假设语气、转述都不算可核事实。

本模块不直接写库：save_candidates 负责落库并保留来源，service 层调用它。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: 允许的记忆类型。模型只能从这四个里选。
MEMORY_KINDS: tuple[str, ...] = ("event", "preference", "profile", "relation")

#: 来源标记（会原样写进 memory.source，之后可查）。
SOURCE_MODEL = "model-extractor"
SOURCE_RULE = "m0-rule-extractor"
#: 模型不可用/不可信、改由规则兜底时的来源标记。
SOURCE_RULE_FALLBACK = "rule-fallback"

#: 单条记忆正文的长度上限，防止模型把整段对话塞成一条。
CONTENT_MAX_CHARS = 120


@dataclass(slots=True)
class MemoryCandidate:
    """一条待写入的记忆候选。

    source_event_id 指向触发它的事件；reason 只用于调试与降级说明，
    不写进 memory 表。
    """

    kind: str
    who: str
    content: str
    weight: float
    source_event_id: str | None = None
    reason: str = ""


@dataclass(slots=True)
class ExtractionResult:
    """一次抽取的完整结果：候选列表 + 用了哪条路 + 降级原因（若有）。

    degraded=True 时 source 一定是 rule-fallback，这是唯一允许的降级标签；
    不允许谎报成 model-extractor。
    """

    candidates: list[MemoryCandidate] = field(default_factory=list)
    source: str = SOURCE_RULE
    degraded: bool = False
    reason: str = ""
    model_name: str = ""
    raw_model_output: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "degraded": self.degraded,
            "reason": self.reason,
            "model": self.model_name,
            "count": len(self.candidates),
            "candidates": [
                {
                    "kind": c.kind,
                    "who": c.who,
                    "content": c.content,
                    "weight": c.weight,
                    "source_event_id": c.source_event_id,
                    "reason": c.reason,
                }
                for c in self.candidates
            ],
        }


# ---------------------------------------------------------------------------
# 可核性：疑问句 / 引用 / 假设都不能写成事实
# ---------------------------------------------------------------------------

# 允许中间插入副词（"我也讨厌"、"我挺喜欢"、"我特别喜欢"）。
_ADVERB = r"(?:也|还|都|挺|很|特别|超级|比较|有点|真的|不太|不怎么)?"
_NAME = re.compile(r"(?:我叫|叫我)\s*([\w\u4e00-\u9fff·_-]{1,24})")
_NEGATIVE = re.compile(
    r"我" + _ADVERB + r"(?:不喜欢|讨厌|受不了)\s*([^，。！？；,.!?;\n]{1,60})"
)
_LIKE = re.compile(r"我" + _ADVERB + r"(?<!不)喜欢\s*([^，。！？；,.!?;\n]{1,60})")

# 疑问、猜测、引用都不是可核事实，不能写进长期记忆。
_INTERROGATIVE = ("什么", "啥", "吗", "呢", "哪", "谁", "怎么", "多少", "几", "？", "?")
_QUOTED = ("“", "”", "「", "」", chr(34), "『", "』")

# task-15 扩展：假设/传闻/传闻式转述同样不是事实。
_SPECULATIVE = (
    "如果",
    "假如",
    "要是",
    "也许",
    "可能",
    "大概",
    "据说",
    "听说",
    "别人说",
    "他说的",
    "她说的",
    "是不是",
    "要不要",
)

#: 模型输出里常见的空泛内容，不构成记忆。
_EMPTYISH = ("", "无", "没有", "暂无", "null", "none", "n/a")


def _is_fact_like(value: str) -> bool:
    """拒绝疑问句、空白和明显是引用的片段，避免把问句存成事实。"""
    value = value.strip()
    if not value:
        return False
    if any(token in value for token in _INTERROGATIVE):
        return False
    if any(token in value for token in _QUOTED):
        return False
    return True


def is_checkable_fact(value: str) -> bool:
    """在 _is_fact_like 之上再挡掉假设、传闻与空泛内容（模型抽取用）。"""
    text = (value or "").strip()
    if not _is_fact_like(text):
        return False
    if text.lower() in _EMPTYISH:
        return False
    if any(token in text for token in _SPECULATIVE):
        return False
    return True



def extract_memories(message: str) -> list[MemoryCandidate]:
    """M0 的确定性事实提取；现在作为快速路径与模型不可用时的兜底。

    保持原有行为与权重不变（既有测试依赖它）；新增的只有 source_event_id 与 reason
    两个可选字段，默认值不影响既有调用方。
    """
    candidates: list[MemoryCandidate] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, content: str, weight: float) -> None:
        key = (kind, content)
        if key not in seen:
            seen.add(key)
            candidates.append(MemoryCandidate(kind, "主人", content, weight))

    if match := _NAME.search(message):
        name = match.group(1).strip()
        if _is_fact_like(name):
            add("profile", f"主人希望被称呼为{name}", 0.95)

    negative_spans: list[tuple[int, int]] = []
    for match in _NEGATIVE.finditer(message):
        value = match.group(1)
        negative_spans.append(match.span())
        if _is_fact_like(value):
            add("preference", f"主人不喜欢{value.strip()}", 0.8)

    for match in _LIKE.finditer(message):
        # 已被负面模式覆盖的片段不再当作正面偏好。
        if any(start < match.end() and match.start() < end for start, end in negative_spans):
            continue
        value = match.group(1)
        if _is_fact_like(value):
            add("preference", f"主人喜欢{value.strip()}", 0.8)

    return candidates


# ---------------------------------------------------------------------------
# 模型结构化抽取
# ---------------------------------------------------------------------------

#: 抽取提示词。只输出 JSON 数组，不解释、不寒暄。
EXTRACT_SYSTEM = """你是灵鲸鲸的记忆整理员。读主人刚说的一句话，只提取**可以核对的事实**。

只输出一个 JSON 数组，不要任何解释、不要代码块围栏。每个元素形如：
{"kind": "preference", "who": "主人", "content": "主人喜欢蓝鲸", "weight": 0.8}

kind 只能是这四个之一：
- event：发生过的具体事情（"主人今天加班到十点"）
- preference：偏好与厌恶（"主人喜欢蓝鲸"、"主人不喜欢熬夜"）
- profile：身份、称呼、住地、习惯等长期属性
- relation：与别的人或物的关系（"主人养了一只叫豆豆的猫"）

规则：
1. content 用第三人称写完整一句，主语用 who。不要照抄原话里的引号。
2. weight 取 0~1：长期稳定的 0.8~0.95，一次性的 0.3~0.6。
3. **疑问句、假设、转述别人的话、引用的台词，一律不要提取。**
   例："你还记得我喜欢什么吗？" → []；"他说他喜欢猫" → []。
4. 没有可核事实就输出 []。宁可不记，也不要编造。"""




def build_extract_prompt(message: str) -> str:
    """把用户消息包进抽取提示；单独成函数便于测试与调试接口复用。"""
    return f"主人说：{json.dumps(message, ensure_ascii=False)}\n\n只输出 JSON 数组。"


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def parse_model_output(raw: str) -> list[dict[str, Any]]:
    """把模型返回解析成字典列表。解析不出来就抛 ValueError（由调用方降级）。

    容忍常见小毛病：代码块围栏、前后多余文字、单对象而非数组。
    """
    text = _FENCE.sub("", (raw or "").strip()).strip()
    if not text:
        raise ValueError("模型返回了空内容")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 从混杂文本里抠出第一段 JSON 数组或对象
        start = min(
            (i for i in (text.find("["), text.find("{")) if i >= 0),
            default=-1,
        )
        if start < 0:
            raise ValueError("模型输出里找不到 JSON")
        end = max(text.rfind("]"), text.rfind("}"))
        if end <= start:
            raise ValueError("模型输出的 JSON 不完整")
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"模型输出的 JSON 无法解析：{exc}") from exc
    if isinstance(data, dict):
        # 也接受 {"memories": [...]} 这种包装
        for key in ("memories", "items", "facts", "results"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        raise ValueError("模型输出的 JSON 不是数组")
    return [item for item in data if isinstance(item, dict)]


def _coerce_weight(value: Any, kind: str) -> float:
    """weight 取不到或越界时按类型给合理解释，而不是硬塞 0 或 1。"""
    default = {"profile": 0.9, "preference": 0.8, "relation": 0.7, "event": 0.4}.get(kind, 0.5)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number < 0 or number > 1:
        return default
    return round(number, 3)


def _content_grounded(content: str, message: str) -> bool:
    """模型写的内容必须在原文里找得到依据，否则视为编造。

    判定方式（任一成立即可）：
    1. 去引号的正文整句出现在原文里；
    2. 正文里有至少一个非"主人/灵鲸鲸"的实词出现在原文里。

    为什么不用"实词命中率"：中文回忆句常被模型概括成"主人喜欢X"，
    主语和谓语都是模型补的，只有 X 来自原文 —— 按比例算会把真话判成编造。
    这里宁可放宽一点，因为真正的防编造闸门是上一步的 is_checkable_fact
    （疑问/引用/假设）以及**只有用户亲口说的那句才会触发抽取**这个前置条件。
    """
    source = re.sub(r"\s+", "", message or "")
    if not source:
        return False
    plain = re.sub(r"[\s，。！？；,.!?;\u201c\u201d\u300c\u300d\u300e\u300f]", "", content or "")
    if plain and plain in source:
        return True
    # 中文没有空格，逐字滑窗找出"内容里出现过、原文里也出现过"的连续片段。
    # 长度 2 起算，避免单字（"我""的"）造成误判。
    clean = re.sub(r"[\s，。！？；,.!?;\u201c\u201d\u300c\u300d\u300e\u300f]", "", content or "")
    for size in (4, 3, 2):
        for index in range(0, max(0, len(clean) - size + 1)):
            window = clean[index : index + size]
            if any(ch in window for ch in "我的了是在有和"):
                continue
            if window in source:
                return True
    return False


def normalize_candidates(
    items: list[dict[str, Any]],
    message: str,
    *,
    source_event_id: str | None = None,
    rejected: list[dict[str, str]] | None = None,
) -> list[MemoryCandidate]:
    """把模型吐出的原始条目转成可写的候选，逐条做过核性检查。

    被拒的条目记进 rejected（调试接口会回显），不静默吞掉。
    """
    out: list[MemoryCandidate] = []
    seen: set[tuple[str, str]] = set()

    def reject(index: int, code: str, message_text: str) -> None:
        if rejected is not None:
            rejected.append({"index": str(index), "code": code, "message": message_text})

    for index, item in enumerate(items):
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in MEMORY_KINDS:
            reject(index, "bad_kind", f"kind 只能是 {MEMORY_KINDS}，收到 {kind!r}")
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            reject(index, "empty_content", "content 为空")
            continue
        if len(content) > CONTENT_MAX_CHARS:
            content = content[:CONTENT_MAX_CHARS].rstrip()
        if not is_checkable_fact(content):
            reject(index, "not_checkable", f"不是可核事实（疑问/引用/假设/空泛）：{content}")
            continue
        if not _content_grounded(content, message):
            reject(index, "not_grounded", f"原文里找不到依据，疑似编造：{content}")
            continue
        who = str(item.get("who") or "主人").strip() or "主人"
        key = (kind, content)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            MemoryCandidate(
                kind=kind,
                who=who,
                content=content,
                weight=_coerce_weight(item.get("weight"), kind),
                source_event_id=source_event_id,
                reason="model",
            )
        )
    return out


# ---------------------------------------------------------------------------
# 异步入口：模型抽取，失败就降级到规则（并把降级写清楚）
# ---------------------------------------------------------------------------


def _rule_fallback(
    message: str,
    *,
    source_event_id: str | None,
    reason: str,
    model_name: str = "",
    raw: str = "",
) -> ExtractionResult:
    """兜底：用规则提，但把 source 明确标成 rule-fallback，不冒充模型抽取。"""
    candidates = extract_memories(message)
    for candidate in candidates:
        candidate.source_event_id = source_event_id
        candidate.reason = "rule-fallback"
    return ExtractionResult(
        candidates=candidates,
        source=SOURCE_RULE_FALLBACK,
        degraded=True,
        reason=reason,
        model_name=model_name,
        raw_model_output=raw,
    )


async def extract_memories_async(
    model: Any,
    message: str,
    *,
    source_event_id: str | None = None,
    rejected: list[dict[str, str]] | None = None,
) -> ExtractionResult:
    """用 ChatModel 做一次结构化抽取；任何异常都降级成 rule-fallback。

    降级条件（全部写进 reason，不隐藏）：
    - 模型没有 chat 方法 / 抛异常（ModelUnavailable 等）
    - 返回不是合法 JSON
    - 返回了合法 JSON 但**一条可核事实都没有**且规则也提不出东西

    注意：模型正常返回 []（明确表示没有事实）**不算降级** —— 那是可信的空结果。
    """
    model_name = str(getattr(model, "name", "") or type(model).__name__)
    if not hasattr(model, "chat"):
        return _rule_fallback(
            message, source_event_id=source_event_id, reason="没有可用的 ChatModel"
        )

    try:
        raw = await model.chat(
            [{"role": "user", "content": build_extract_prompt(message)}],
            EXTRACT_SYSTEM,
        )
    except Exception as exc:  # noqa: BLE001 - 模型挂了必须降级而不是炸掉对话
        logger.warning("记忆抽取模型调用失败，降级到规则：%s", exc)
        return _rule_fallback(
            message,
            source_event_id=source_event_id,
            reason=f"模型调用失败：{type(exc).__name__}: {exc}",
            model_name=model_name,
        )

    try:
        items = parse_model_output(raw if isinstance(raw, str) else str(raw))
    except ValueError as exc:
        logger.warning("记忆抽取模型输出无法解析，降级到规则：%s", exc)
        return _rule_fallback(
            message,
            source_event_id=source_event_id,
            reason=f"模型输出不可解析：{exc}",
            model_name=model_name,
            raw=str(raw)[:500],
        )

    candidates = normalize_candidates(
        items, message, source_event_id=source_event_id, rejected=rejected
    )
    return ExtractionResult(
        candidates=candidates,
        source=SOURCE_MODEL,
        degraded=False,
        reason="" if items else "模型认为这句话没有可核事实",
        model_name=model_name,
        raw_model_output=str(raw)[:2000],
    )


def save_candidates(
    db: Any,
    candidates: list[MemoryCandidate],
    *,
    source: str,
    source_event_id: str | None,
) -> list[dict[str, Any]]:
    """把候选写进 memory 表，source 与 source_event_id 原样保留，便于追溯。

    写库失败不抛：单条坏了跳过并记录，不影响对话主流程，也不假装写成功。
    """
    saved: list[dict[str, Any]] = []
    for candidate in candidates:
        try:
            saved.append(
                db.add_memory(
                    candidate.kind,
                    candidate.who,
                    candidate.content,
                    candidate.weight,
                    source,
                    source_event_id or candidate.source_event_id,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("记忆写入失败（%s）：%s", candidate.content[:40], exc)
    return saved
