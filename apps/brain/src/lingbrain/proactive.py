"""M6 主动话题与打扰预算。

只做一件事：在**值得且被允许**的时候，由她主动开一句口。

三根支柱
--------

1. **打扰预算**（立项书：主动行为有额度，不能刷屏）
   - 每天最多 3 条，按**本地日**切分（默认 UTC+8，与 state_machine 一致）。
   - 两条之间至少隔 :data:`MIN_GAP_MINUTES` 分钟，避免一口气说完。
   - 每条成功发出才计预算；被拦下的**不占额度**，但要留下记录（不隐藏失败）。

2. **静默时段**（唯一真相来源是 state_machine）
   - 每次开口前必问 ``gate.proactive_hint()``，并在"没有其它理由"时再问一次
     ``gate.should_speak()`` 做交叉校验 —— 与 share.py 保持同一套做法。
   - 静默时段 / 睡着了 / 饿坏了 / 疲惫 → **不发**，原因写进 blocked_reasons。
   - 状态机不可用时走 _FallbackGate：**默认不主动**，宁可不说。

3. **话题来源**（每个候选都带出处，不编造）
   - ``memory_revisit``   记忆回访：把重要记忆里很久没提过的一条重新拿出来。
   - ``share_queue``      分享队列：share 模块里已经攒好、但还没主动推的一批。
   - ``state_reminder``   状态提醒：她饿了 / 困了 / 能量低了，可以自己说一句。
   - ``reunion``          久别重逢：主人很久没联系，她可以主动打个招呼。
   没有可用话题时**不发**（不硬凑、不编造共同经历）。

事件留痕（沿用 append-only 的 event 表，不建新表、不改 db.py）
--------------------------------------------------------------

- ``proactive.sent``    成功主动发出一条，payload 含 day / topic / text / source。
- ``proactive.blocked`` 被预算/静默/开关/无话题拦下，payload 写明具体原因。
- ``proactive.disabled`` 开关关闭时调用，明确记录"没有发"。

原则：**不隐藏降级与失败**。所有返回值都带 ``sent=False`` + ``blocked_reasons``，
调用方不能把"没发"误当成"发了"。mock 话题（无真实来源时的占位）永远不冒充真实记忆。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Protocol, Sequence

from .db import Database

logger = logging.getLogger(__name__)

#: 事件 kind
EVENT_SENT = "proactive.sent"
EVENT_BLOCKED = "proactive.blocked"
EVENT_DISABLED = "proactive.disabled"
EVENT_PREFIX = "proactive."

#: 每天最多主动几次（立项书：每天主动 ≤3 次）
DEFAULT_DAILY_LIMIT = 3
#: 两条主动消息之间至少间隔（分钟），防止一口气发完额度
DEFAULT_MIN_GAP_MINUTES = 90
#: 默认本地日偏移（与 state_machine / share 一致）
DEFAULT_UTC_OFFSET_MINUTES = 8 * 60

#: 开关环境变量
ENV_ENABLED = "LING_PROACTIVE_ENABLED"
#: 预算环境变量
ENV_DAILY_LIMIT = "LING_PROACTIVE_DAILY_LIMIT"
#: 间隔环境变量
ENV_MIN_GAP = "LING_PROACTIVE_MIN_GAP_MINUTES"

#: 久别重逢判定：多久没联系算"久别"
DEFAULT_REUNION_HOURS = 24.0
#: 记忆回访：多久没被提起的记忆才值得回访
DEFAULT_MEMORY_REVISIT_DAYS = 7.0
#: 主动消息长度上限（一句，就一句）
TEXT_MAX_CHARS = 200

#: 正向开关的取值（其它一律当关闭）
_TRUTHY = {"1", "true", "yes", "on", "enabled"}


def _tz(minutes: int | None = None) -> timezone:
    return timezone(timedelta(minutes=minutes if minutes is not None else DEFAULT_UTC_OFFSET_MINUTES))


def local_day(now: datetime | None = None, *, offset_minutes: int | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(_tz(offset_minutes)).strftime("%Y-%m-%d")


def parse_enabled(value: Any, *, default: bool = True) -> bool:
    """把环境变量/配置解析成布尔。未设置时用 default（默认开启）。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text == "":
        return default
    return text in _TRUTHY


# ---------------------------------------------------------------------------
# 闸门：只需要 state_machine 的两个方法
# ---------------------------------------------------------------------------


class ProactiveGate(Protocol):
    """只需要这两个方法，方便测试替换。state_machine.StateMachine 天然满足。"""

    def should_speak(self, now: datetime | None = None) -> bool: ...

    def proactive_hint(self, now: datetime | None = None) -> dict[str, Any]: ...


class FallbackGate:
    """没有状态机时的保守闸门：默认**不主动**，并把原因写清楚。

    保守方向是有意的：拿不准的时候不发，比发出去打扰主人更好。
    """

    def should_speak(self, now: datetime | None = None) -> bool:
        return False

    def proactive_hint(self, now: datetime | None = None) -> dict[str, Any]:
        return {
            "allowed": False,
            "reasons": ["状态机不可用，无法判断静默时段与睡眠，按不主动处理"],
            "state": {},
        }


# ---------------------------------------------------------------------------
# 话题
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Topic:
    """一条候选话题。``text`` 是准备说出口的那句话，``source`` 说明它从哪来。"""

    kind: str  # memory_revisit / share_queue / state_reminder / reunion
    text: str
    source: str  # 人能读懂出处的短说明
    evidence_id: str | None = None  # 记忆 id / 事件 id，方便追溯
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "source": self.source,
            "evidence_id": self.evidence_id,
            **self.extra,
        }


TOPIC_KINDS = ("memory_revisit", "share_queue", "state_reminder", "reunion")


def _shorten(text: str, limit: int = 60) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# 话题来源
# ---------------------------------------------------------------------------


class TopicSource(Protocol):
    """话题来源接口。返回 None 表示"这次没有可说的"，不要编造。"""

    kind: str

    def propose(self, now: datetime | None = None) -> Topic | None: ...


class StateReminderSource:
    """状态提醒：她饿了 / 困了 / 能量快没了，可以自己说一句。"""

    kind = "state_reminder"

    def __init__(self, gate: ProactiveGate):
        self.gate = gate

    def propose(self, now: datetime | None = None) -> Topic | None:
        hint = self.gate.proactive_hint(now)
        state = hint.get("state") or {}
        satiety = state.get("satiety")
        fatigue = state.get("fatigue")
        energy = state.get("energy")
        mood = state.get("mood")

        if isinstance(satiety, (int, float)) and satiety <= 15:
            return Topic(
                kind=self.kind,
                text="我有点饿了，你忙完记得给自己也弄点吃的。",
                source=f"状态：饱食 {satiety}",
                extra={"metric": "satiety", "value": satiety},
            )
        if isinstance(fatigue, (int, float)) and fatigue >= 85:
            return Topic(
                kind=self.kind,
                text="我有点困了，可能要早点睡。你也别熬太晚。",
                source=f"状态：困倦 {fatigue}",
                extra={"metric": "fatigue", "value": fatigue},
            )
        if isinstance(energy, (int, float)) and energy <= 15:
            return Topic(
                kind=self.kind,
                text="我快没电了，先去歇一会儿。",
                source=f"状态：能量 {energy}",
                extra={"metric": "energy", "value": energy},
            )
        if mood in {"饿坏了", "疲惫"}:
            return Topic(
                kind=self.kind,
                text=f"我现在是{mood}，想休息一下。",
                source=f"状态：心情 {mood}",
                extra={"metric": "mood", "value": mood},
            )
        return None


class MemoryRevisitSource:
    """记忆回访：把一条重要、但很久没被提起的记忆重新拿出来。

    只用 db 里真实存在的记忆，**不编造**。找不到合适的就是 None。
    """

    kind = "memory_revisit"

    def __init__(self, db: Database, revisit_days: float = DEFAULT_MEMORY_REVISIT_DAYS):
        self.db = db
        self.revisit_days = float(revisit_days)

    def _recall(self) -> set[str]:
        """最近主动回访过的记忆 id，避免同一条反复拿出来。"""
        recalled: set[str] = set()
        for event in self.db.list_events(limit=400):
            if event["kind"] == EVENT_SENT:
                evidence = (event["payload"].get("evidence_id") or "")
                if evidence:
                    recalled.add(str(evidence))
        return recalled

    def propose(self, now: datetime | None = None) -> Topic | None:
        moment = now or datetime.now(timezone.utc)
        floor = moment - timedelta(days=self.revisit_days)
        recalled = self._recall()
        try:
            memories = self.db.important_memories(limit=20)
        except Exception as exc:  # noqa: BLE001 - 读失败就不主动，不隐藏
            logger.warning("读取记忆失败，跳过记忆回访：%s", exc)
            return None
        for memory in memories:
            content = str(memory.get("content") or "").strip()
            if not content or memory["id"] in recalled:
                continue
            try:
                ts = datetime.fromisoformat(str(memory.get("ts")))
            except (TypeError, ValueError):
                ts = None
            if ts is not None and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts is not None and ts > floor:
                continue  # 最近才记下的，还不用回访
            return Topic(
                kind=self.kind,
                text=f"突然想起一件事：{_shorten(content)}。还记得吗？",
                source=f"记忆：{_shorten(content, 30)}",
                evidence_id=str(memory["id"]),
                extra={"memory_kind": memory.get("kind"), "weight": memory.get("weight")},
            )
        return None


class ReunionSource:
    """久别重逢：主人很久没说话了，她可以主动打个招呼。"""

    kind = "reunion"

    def __init__(self, db: Database, reunion_hours: float = DEFAULT_REUNION_HOURS):
        self.db = db
        self.reunion_hours = float(reunion_hours)

    def _last_user_message(self) -> datetime | None:
        for event in self.db.list_events(limit=200):
            if event["kind"] == "user.message":
                try:
                    ts = datetime.fromisoformat(str(event["ts"]))
                except ValueError:
                    continue
                return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        return None

    def _greeted_since(self, since: datetime) -> bool:
        for event in self.db.list_events(limit=200):
            if event["kind"] != EVENT_SENT:
                continue
            if event["payload"].get("topic") != self.kind:
                continue
            try:
                ts = datetime.fromisoformat(str(event["ts"]))
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= since:
                return True
        return False

    def propose(self, now: datetime | None = None) -> Topic | None:
        moment = now or datetime.now(timezone.utc)
        last = self._last_user_message()
        if last is None:
            return None
        silence = moment - last
        if silence < timedelta(hours=self.reunion_hours):
            return None
        # 太久没联系只打一次招呼，别每轮都来一句
        if self._greeted_since(last):
            return None
        hours = int(silence.total_seconds() // 3600)
        if hours >= 48:
            text = f"我们好像 {hours // 24} 天没说话了，我在呢。"
        else:
            text = f"你有一阵没来了（{hours} 小时），我在呢。"
        return Topic(
            kind=self.kind,
            text=text,
            source=f"已 {hours} 小时没有收到主人的消息",
            extra={"silence_hours": hours},
        )


class ShareQueueSource:
    """分享队列：share 模块里已经攒好、但还没主动推出去的一批。

    只读地借用 share 服务（可选依赖）；没接 share 时返回 None，不硬凑。
    """

    kind = "share_queue"

    def __init__(self, share_service: Any):
        self.share = share_service

    def propose(self, now: datetime | None = None) -> Topic | None:
        service = self.share
        if service is None:
            return None
        try:
            queued = getattr(service, "queued_items", None)
            if callable(queued):
                items = queued(now) or []
            else:
                items = getattr(service, "queue", None) or []
        except Exception as exc:  # noqa: BLE001 - 读失败不主动，不隐藏
            logger.warning("读取分享队列失败，跳过：%s", exc)
            return None
        if not items:
            return None
        first = items[0]
        if isinstance(first, dict):
            title = first.get("title") or first.get("text") or ""
            key = first.get("key")
        else:
            title = getattr(first, "title", "") or getattr(first, "text", "")
            key = getattr(first, "key", None)
        title = _shorten(str(title), 40)
        if not title:
            return None
        return Topic(
            kind=self.kind,
            text=f"我攒到一个想给你看的东西：{title}",
            source="分享队列里还有没推的一批",
            evidence_id=None if key is None else str(key),
        )


# ---------------------------------------------------------------------------
# 预算与节奏
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BudgetSnapshot:
    day: str
    used: int
    limit: int
    remaining: int
    next_available_at: str | None
    last_sent_at: str | None


class ProactiveService:
    """主动话题服务：预算 + 静默闸门 + 话题选择。"""

    def __init__(
        self,
        db: Database,
        gate: ProactiveGate | None = None,
        *,
        sources: Sequence[TopicSource] | None = None,
        daily_limit: int = DEFAULT_DAILY_LIMIT,
        min_gap_minutes: float = DEFAULT_MIN_GAP_MINUTES,
        utc_offset_minutes: int = DEFAULT_UTC_OFFSET_MINUTES,
        enabled: bool = True,
    ):
        if daily_limit < 0:
            raise ValueError("每日主动次数上限不能为负")
        self.db = db
        self.gate = gate if gate is not None else FallbackGate()
        self.daily_limit = int(daily_limit)
        self.min_gap_minutes = float(min_gap_minutes)
        self.utc_offset_minutes = int(utc_offset_minutes)
        self.enabled = bool(enabled)
        self.sources: list[TopicSource] = list(sources) if sources is not None else self._default_sources()

    def _default_sources(self) -> list[TopicSource]:
        return [
            StateReminderSource(self.gate),
            MemoryRevisitSource(self.db),
            ReunionSource(self.db),
        ]

    # ---- 读：从事件重放 -------------------------------------------------

    def _events(self, limit: int = 400, kind: str | None = None) -> list[dict[str, Any]]:
        rows = self.db.list_events(limit=limit)
        return [
            row
            for row in rows
            if str(row["kind"]).startswith(EVENT_PREFIX)
            and (kind is None or row["kind"] == kind)
        ]

    def sent_today(self, now: datetime | None = None) -> int:
        """今天（本地日）已经主动发出去几条。只数真正发出去的。"""
        today = local_day(now, offset_minutes=self.utc_offset_minutes)
        return sum(
            1
            for event in self._events(kind=EVENT_SENT)
            if event["payload"].get("day") == today
        )

    def last_sent_at(self, now: datetime | None = None) -> datetime | None:
        """最近一次真正发出去的时刻。

        优先用事件里记下的 ``sent_at``（发起者传入的 ``now`），
        回退到 event.ts。这样时间推进在测试/回放里可控，也不会因为
        落库时刻和业务时刻有几毫秒差而算错间隔。
        """
        for event in self._events(kind=EVENT_SENT):
            raw = event["payload"].get("sent_at") or event["ts"]
            try:
                ts = datetime.fromisoformat(str(raw))
            except ValueError:
                continue
            return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        return None

    def budget(self, now: datetime | None = None) -> BudgetSnapshot:
        moment = now or datetime.now(timezone.utc)
        used = self.sent_today(moment)
        remaining = max(0, self.daily_limit - used)
        next_at: str | None = None
        last = self.last_sent_at(moment)
        if remaining > 0 and last is not None:
            ready = last + timedelta(minutes=self.min_gap_minutes)
            if ready > moment:
                next_at = ready.isoformat(timespec="seconds")
        return BudgetSnapshot(
            day=local_day(moment, offset_minutes=self.utc_offset_minutes),
            used=used,
            limit=self.daily_limit,
            remaining=remaining,
            next_available_at=next_at,
            last_sent_at=None if last is None else last.isoformat(timespec="seconds"),
        )

    # ---- 闸门 ----------------------------------------------------------

    def _hint(self, now: datetime | None = None) -> dict[str, Any]:
        try:
            return self.gate.proactive_hint(now) or {}
        except Exception as exc:  # noqa: BLE001 - 状态机坏了就按不主动处理
            return {"allowed": False, "reasons": [f"状态机调用失败：{exc}"], "state": {}}

    def can_speak(self, now: datetime | None = None) -> tuple[bool, list[str]]:
        """能不能主动开口。返回 (允许?, 原因列表)；允许时原因列表为空。"""
        if not self.enabled:
            return False, ["主动话题已被关闭（LING_PROACTIVE_ENABLED=0）"]
        reasons: list[str] = []
        hint = self._hint(now)
        if not hint.get("allowed"):
            reasons.extend(hint.get("reasons") or ["当前不适合主动开口"])
        # 静默时段不主动：should_speak() 是唯一真相来源，这里再问一次交叉校验。
        try:
            if not self.gate.should_speak(now) and not reasons:
                reasons.append("should_speak() 判定现在不该主动")
        except Exception as exc:  # noqa: BLE001
            reasons.append(f"should_speak() 调用失败：{exc}")

        moment = now or datetime.now(timezone.utc)
        budget = self.budget(moment)
        if budget.used >= self.daily_limit:
            reasons.append(
                f"今天已经主动说了 {budget.used} 次，上限 {self.daily_limit} 次"
            )
        else:
            # 间距判定用时间点直接比较：next_available_at 为空只代表"没有待等待的间隔"，
            # 不代表"刚发过"。
            last = self.last_sent_at(moment)
            if last is not None:
                ready = last + timedelta(minutes=self.min_gap_minutes)
                if ready > moment:
                    wait = int((ready - moment).total_seconds() // 60) + 1
                    reasons.append(
                        f"距上一条主动消息不足 {int(self.min_gap_minutes)} 分钟，还需等 {wait} 分钟"
                    )
        return (not reasons, reasons)

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        moment = now or datetime.now(timezone.utc)
        budget = self.budget(moment)
        allowed, reasons = self.can_speak(moment)
        topic = self.pick_topic(moment) if allowed else None
        return {
            "enabled": self.enabled,
            "day": budget.day,
            "used_today": budget.used,
            "daily_limit": budget.limit,
            "remaining": budget.remaining,
            "next_available_at": budget.next_available_at,
            "last_sent_at": budget.last_sent_at,
            "min_gap_minutes": self.min_gap_minutes,
            "allowed": allowed,
            "blocked_reasons": reasons,
            "in_quiet_hours": bool((self._hint(moment).get("state") or {}).get("in_quiet_hours")),
            "next_topic": None if topic is None else topic.to_dict(),
            "topic_sources": [getattr(s, "kind", "unknown") for s in self.sources],
            "recent": self.history(limit=5),
        }

    # ---- 话题选择 ------------------------------------------------------

    def pick_topic(self, now: datetime | None = None) -> Topic | None:
        """按顺序问每个来源要一条；都用不了就返回 None（不硬凑）。"""
        for source in self.sources:
            try:
                topic = source.propose(now)
            except Exception as exc:  # noqa: BLE001 - 单个来源坏了不影响其它
                logger.warning("话题来源 %s 失败：%s", getattr(source, "kind", "?"), exc)
                continue
            if topic is not None:
                return topic
        return None

    # ---- 发 ------------------------------------------------------------

    def tick(self, now: datetime | None = None, *, client_id: str = "system") -> dict[str, Any]:
        """做一次判断：能发就发一条，不能发就明确记下原因。返回完整结果。"""
        moment = now or datetime.now(timezone.utc)
        budget = self.budget(moment)

        if not self.enabled:
            self.db.append_event(
                "灵鲸鲸",
                EVENT_DISABLED,
                {"day": budget.day, "reason": "主动话题被关闭", "client_id": client_id},
            )
            return {
                "sent": False,
                "topic": None,
                "blocked_reasons": ["主动话题已被关闭（LING_PROACTIVE_ENABLED=0）"],
                "budget": self._budget_dict(budget),
                "enabled": False,
            }

        allowed, reasons = self.can_speak(moment)
        if not allowed:
            self.db.append_event(
                "灵鲸鲸",
                EVENT_BLOCKED,
                {
                    "day": budget.day,
                    "reasons": reasons,
                    "client_id": client_id,
                    "budget": self._budget_dict(budget),
                },
            )
            return {
                "sent": False,
                "topic": None,
                "blocked_reasons": reasons,
                "budget": self._budget_dict(budget),
                "enabled": True,
            }

        topic = self.pick_topic(moment)
        if topic is None:
            reasons = ["暂时没有值得主动说的话题"]
            self.db.append_event(
                "灵鲸鲸",
                EVENT_BLOCKED,
                {"day": budget.day, "reasons": reasons, "client_id": client_id, "budget": self._budget_dict(budget)},
            )
            return {
                "sent": False,
                "topic": None,
                "blocked_reasons": reasons,
                "budget": self._budget_dict(budget),
                "enabled": True,
            }

        text = str(topic.text).strip()
        if len(text) > TEXT_MAX_CHARS:
            text = text[: TEXT_MAX_CHARS - 1] + "…"
        event = self.db.append_event(
            "灵鲸鲸",
            EVENT_SENT,
            {
                "day": local_day(moment, offset_minutes=self.utc_offset_minutes),
                "sent_at": moment.isoformat(timespec="milliseconds"),
                "topic": topic.kind,
                "text": text,
                "source": topic.source,
                "evidence_id": topic.evidence_id,
                "client_id": client_id,
                "budget_after": {"used": budget.used + 1, "limit": budget.limit},
                **topic.extra,
            },
        )
        after = self.budget(moment)
        return {
            "sent": True,
            "topic": {"kind": topic.kind, "text": text, "source": topic.source,
                      "evidence_id": topic.evidence_id},
            "event_id": event["id"],
            "blocked_reasons": [],
            "budget": self._budget_dict(after),
            "enabled": True,
        }

    def _budget_dict(self, budget: BudgetSnapshot) -> dict[str, Any]:
        return {
            "day": budget.day,
            "used": budget.used,
            "limit": budget.limit,
            "remaining": budget.remaining,
            "next_available_at": budget.next_available_at,
            "last_sent_at": budget.last_sent_at,
        }

    # ---- 历史 ----------------------------------------------------------

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        """最近的主动相关事件（发出/被拦/关闭），最新在前。"""
        return self._events(limit=max(limit * 4, 100))[: max(1, limit)]


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def build_proactive_service(
    db: Database,
    gate: ProactiveGate | None = None,
    settings: Any | None = None,
    *,
    share_service: Any = None,
) -> ProactiveService:
    """从 Settings / 环境变量组装。

    * ``LING_PROACTIVE_ENABLED=0/false/off`` 关闭主动（默认开启）。
    * ``LING_PROACTIVE_DAILY_LIMIT`` 覆盖每日上限（默认 3）。
    * ``LING_PROACTIVE_MIN_GAP_MINUTES`` 覆盖两条之间的最小间隔。
    """
    def _get(key: str) -> Any:
        if settings is None:
            return None
        return getattr(settings, key, None)

    enabled = parse_enabled(
        os.getenv(ENV_ENABLED) if os.getenv(ENV_ENABLED) is not None else _get("proactive_enabled"),
        default=True,
    )
    limit_raw = os.getenv(ENV_DAILY_LIMIT) or _get("proactive_daily_limit")
    gap_raw = os.getenv(ENV_MIN_GAP) or _get("proactive_min_gap_minutes")
    offset = _get("state_utc_offset_minutes") or DEFAULT_UTC_OFFSET_MINUTES

    sources: list[TopicSource] = []
    if gate is not None:
        sources.append(StateReminderSource(gate))
    sources.append(MemoryRevisitSource(db))
    sources.append(ReunionSource(db))
    if share_service is not None:
        sources.append(ShareQueueSource(share_service))

    return ProactiveService(
        db,
        gate,
        sources=sources,
        daily_limit=int(limit_raw) if limit_raw else DEFAULT_DAILY_LIMIT,
        min_gap_minutes=float(gap_raw) if gap_raw else DEFAULT_MIN_GAP_MINUTES,
        utc_offset_minutes=int(offset),
        enabled=enabled,
    )


__all__ = [
    "ProactiveService",
    "ProactiveGate",
    "FallbackGate",
    "Topic",
    "TopicSource",
    "StateReminderSource",
    "MemoryRevisitSource",
    "ReunionSource",
    "ShareQueueSource",
    "BudgetSnapshot",
    "build_proactive_service",
    "parse_enabled",
    "local_day",
    "TOPIC_KINDS",
    "EVENT_SENT",
    "EVENT_BLOCKED",
    "EVENT_DISABLED",
    "EVENT_PREFIX",
    "DEFAULT_DAILY_LIMIT",
    "DEFAULT_MIN_GAP_MINUTES",
    "ENV_ENABLED",
]
