"""灵鲸鲸 M1 状态机：能量/饱食/困倦随时间衰减，心情、在场与静默时段。

设计原则
--------
* **时间驱动，而不是轮询驱动**：所有衰减都按"距离上一次推进过去了多少分钟"计算，
  因此进程睡了一夜再用一份旧快照也不会有偏差；真正落库只发生在 heartbeat、
  互动、显式刷新这些时刻。
* **状态表复用**：全部字段都存在既有的 ``state`` 表（``db.set_state`` / ``db.get_state``），
  不新建表；新增字段只在服务端内部使用，``/v1/state`` 对外一律是"只加字段"。
* **事件日志复用**：每一次真实推进都写一条 ``state.changed`` 事件（走 ``db.append_event``，
  天然 append-only），状态变更史直接从 ``event`` 表读。
* **可注入时钟**：``now`` 参数贯穿所有入口，测试可以精确控制时间，不睡真实秒数。
* **不依赖 tzdata**：静默时段按固定 UTC 偏移计算（默认 +8 区），
  Windows 上没有 tzdata 包时 ``zoneinfo`` 不可用，这里刻意避开它。

字段说明
--------
+----------+----------------------------------------------+---------+
| 字段     | 含义                                         | 范围    |
+==========+==============================================+=========+
| energy   | 能量。醒着每 ENERGY_WAKE_DECAY_PER_MIN 掉一点| 0..100  |
|          | 睡着每 ENERGY_SLEEP_RECOVER_PER_MIN 回一点   |         |
| satiety  | 饱食（0 饿 / 100 饱）。醒着时缓慢下降        | 0..100  |
| fatigue  | 困倦（0 不困 / 100 困死）。醒着时缓慢上升    | 0..100  |
| affection| 好感，拖拽/摸摸会涨，冷落会掉                 | 0..100  |
| mood     | 心情，见 MOODS                               | 中文字面|
| presence | 在场，见 PRESENCES                           | 中文字面|
| sleeping | 是否睡着                                     | bool    |
+----------+----------------------------------------------+---------+

``energy`` / ``mood`` / ``presence`` / ``sleeping`` 是 M0 就有的字段，语义保持不变，
只是分布更细、来源更多；其余字段都是新增。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Mapping

from .db import Database

# ---------------------------------------------------------------------------
# 常数（单位：分钟 / 点）。全部做成模块级常量，方便调参与测试引用。
# ---------------------------------------------------------------------------

GAUGE_MIN = 0.0
GAUGE_MAX = 100.0

#: 醒着时能量每分钟下降
ENERGY_WAKE_DECAY_PER_MIN = 0.2
#: 睡着时能量每分钟恢复
ENERGY_SLEEP_RECOVER_PER_MIN = 1.0
#: 睡着时困倦每分钟消退
FATIGUE_SLEEP_RECOVER_PER_MIN = 1.5
#: 醒着时困倦每分钟上升
FATIGUE_WAKE_GAIN_PER_MIN = 0.1
#: 醒着时饱食每分钟下降
SATIETY_DECAY_PER_MIN = 0.08
#: 睡着时饱食每分钟下降（睡觉也会慢慢饿）
SATIETY_SLEEP_DECAY_PER_MIN = 0.04

#: 一次推进最多折算多少分钟，防止离线几十天后能量瞬间清零
MAX_STEP_MINUTES = 8 * 60
#: 单次推进切成的最大片段数；保证片内不跨越睡眠切换也不会算错
MAX_SEGMENTS = 24
#: 自动入睡阈值
AUTO_SLEEP_ENERGY = 12.0
#: 困倦到该程度也会自动入睡
AUTO_SLEEP_FATIGUE = 88.0
#: 困倦低于该值才会自动醒来
AUTO_WAKE_FATIGUE = 5.0
#: 能量回升到该值（且够困倦消退）自动醒来
AUTO_WAKE_ENERGY = 95.0
#: 饱食跌破该值才算"饿了"
HUNGRY_SATIETY = 25.0
#: 饱食跌破该值就算"饿坏了"（外部喂食能救）
STARVING_SATIETY = 0.0

#: 好感，0..100。拖拽、点按会累积；冷落还会掉。
AFFECTION_MIN = 0.0
AFFECTION_MAX = 100.0
AFFECTION_START = 50.0
#: 每天不互动会掉多少好感
AFFECTION_DECAY_PER_DAY = 2.0

#: 每个 client 的互动频率限制（滑动窗口），防止前端画圈时把事件表刷爆
INTERACTION_WINDOW_SECONDS = 60.0
INTERACTION_WINDOW_LIMIT = 60

#: 心情，从最坏到最好排列；mood_rank 取这里面的下标
MOODS: tuple[str, ...] = ("饿坏了", "沮丧", "疲惫", "低落", "有点累", "平静", "开心")

#: 在场状态
PRESENCES: tuple[str, ...] = ("在", "睡了", "打盹", "静默", "离开")

#: 互动类型 -> (能量增减, 好感增减)
INTERACTION_KINDS: dict[str, tuple[float, float]] = {
    "drag": (0.6, 1.5),  # 拖拽：把脑袋挪来挪去，会醒一点点，也开心
    "stroke": (0.4, 2.0),  # 摸头
    "feed": (1.0, 3.0),  # 投喂（饱食另外加）
    "poke": (-0.5, -0.5),  # 戳：被打扰了，会不高兴
    "wake": (-1.0, 0.5),  # 叫醒：有点不情愿
    "play": (0.8, 2.5),  # 逗她玩
}
#: 拖拽会把她晃醒的概率（按能量线性降低）
DRAG_WAKE_CHANCE = 0.3
#: 被拖醒时额外补的能量（"被晃醒了"）
DRAG_WAKE_ENERGY = 3.0
#: 拖拽能持续带来满足感的上限（次），超过后不再涨好感
DRAG_SPOIL_LIMIT = 40
#: 投喂一次加多少饱食
FEED_SATIETY_GAIN = 25.0
#: 互动会让她稍微清醒/困起来（每次 +2 困倦，撒娇也是耗神的）
INTERACTION_FATIGUE_GAIN = 2.0
#: 静默时段里醒着每多待一分钟，额外多掉的困倦
QUIET_FATIGUE_GAIN_PER_MIN = 0.1

#: 所有状态变更都写这个 kind
STATE_EVENT_KIND = "state.changed"
#: 历史接口默认只回这个前缀的事件（与 /v1/events 区分开）
STATE_EVENT_KIND_PREFIX = "state."
#: 互动事件 kind
STATE_INTERACTION_KIND = "state.interaction"

DEFAULT_QUIET_HOURS = "23:30-07:30"
DEFAULT_UTC_OFFSET_MINUTES = 8 * 60
DEFAULT_CITY = "Asia/Shanghai"

#: 参与"有没有变化"比较的字段
_TRACKED_FIELDS = (
    "energy",
    "satiety",
    "fatigue",
    "affection",
    "mood",
    "presence",
    "sleeping",
)


# ---------------------------------------------------------------------------
# 时间工具
# ---------------------------------------------------------------------------


def _normalize_instant(value: datetime) -> datetime:
    """统一成 UTC aware datetime；无 tzinfo 的按 UTC 处理。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_instant(value: Any, default: datetime | None = None) -> datetime | None:
    if isinstance(value, datetime):
        return _normalize_instant(value)
    if not isinstance(value, str) or not value.strip():
        return default
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return _normalize_instant(datetime.fromisoformat(text))
    except ValueError:
        return default


def utc_offset(minutes: float) -> timezone:
    return timezone(timedelta(minutes=int(minutes)))


def parse_local_time(text: str) -> time:
    """把 ``HH:MM`` / ``H:MM`` / ``HHMM`` 解析成 ``datetime.time``。"""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("静默时段里有一个空的时刻")
    if ":" in raw:
        parts = raw.split(":")
    elif len(raw) in (3, 4) and raw.isdigit():
        parts = [raw[:-2], raw[-2:]]
    else:
        parts = [raw, "0"]
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError) as exc:
        raise ValueError(f"看不懂的时刻：{text!r}") from exc
    if hour == 24 and minute == 0:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"时刻越界：{text!r}")
    return time(hour=hour, minute=minute)


@dataclass(frozen=True, slots=True)
class QuietWindow:
    """一个跨零点或同日的静默区间（本地时间，闭开区间 [start, end)）。"""

    start: time
    end: time

    def contains(self, local: datetime) -> bool:
        moment = local.time()
        if self.start == self.end:  # 起止相同 = 整天静默
            return True
        if self.start < self.end:
            return self.start <= moment < self.end
        return moment >= self.start or moment < self.end

    def as_dict(self) -> dict[str, str]:
        return {
            "start": self.start.strftime("%H:%M"),
            "end": self.end.strftime("%H:%M"),
        }


def parse_quiet_hours(spec: str | None) -> list[QuietWindow]:
    """解析 ``"23:30-07:30,13:00-14:00"`` 形式的静默时段。空串表示没有静默时段。"""
    if spec is None:
        spec = DEFAULT_QUIET_HOURS
    windows: list[QuietWindow] = []
    for chunk in spec.replace("，", ",").replace("~", "-").replace("—", "-").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        pieces = chunk.split("-")
        if len(pieces) != 2:
            raise ValueError(f"静默时段写法应为 HH:MM-HH:MM：{chunk!r}")
        windows.append(
            QuietWindow(parse_local_time(pieces[0]), parse_local_time(pieces[1]))
        )
    return windows


def _clamp(value: float, low: float = GAUGE_MIN, high: float = GAUGE_MAX) -> float:
    return max(low, min(high, value))


def _number(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: float) -> int:
    """能量/饱食/困倦对外一直是整数（M0 的契约就是整数 0..100）。"""
    return int(round(value))


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MoodSnapshot:
    """某一时刻换算出来的状态，供 heartbeat / 互动 / API 共用。"""

    internal: dict[str, Any]
    public: dict[str, Any]
    changed: dict[str, Any]


class StateMachine:
    """灵鲸鲸状态机。

    所有入口都接受 ``now``（aware datetime），缺省用当前 UTC；
    时间一律按 UTC 存储，静默时段按 ``utc_offset_minutes`` 换算成本地时间判断。
    """

    def __init__(
        self,
        db: Database,
        *,
        quiet_hours: str | None = DEFAULT_QUIET_HOURS,
        utc_offset_minutes: float = DEFAULT_UTC_OFFSET_MINUTES,
        city: str = DEFAULT_CITY,
    ) -> None:
        self.db = db
        self.quiet_spec = quiet_hours if quiet_hours is not None else ""
        self.quiet_windows = parse_quiet_hours(self.quiet_spec)
        self.utc_offset_minutes = float(utc_offset_minutes)
        self.city = city
        self._tz = utc_offset(self.utc_offset_minutes)

    # -- 时钟 ------------------------------------------------------------

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)

    def to_local(self, instant: datetime) -> datetime:
        return _normalize_instant(instant).astimezone(self._tz)

    def quiet_status(self, now: datetime | None = None) -> dict[str, Any]:
        """``now`` 那一刻的静默时段判断（含用到的本地时间，方便前端核对）。"""
        instant = _normalize_instant(now) if now else self.now()
        local = instant.astimezone(self._tz)
        active = [w.as_dict() for w in self.quiet_windows if w.contains(local)]
        return {
            "in_quiet_hours": bool(active),
            "quiet_hours": self.quiet_spec,
            "active_windows": active,
            "local_time": local.isoformat(timespec="seconds"),
            "utc_offset_minutes": int(self.utc_offset_minutes),
            "city": self.city,
        }

    def in_quiet_hours(self, now: datetime | None = None) -> bool:
        return self.quiet_status(now)["in_quiet_hours"]

    # -- 读状态 ----------------------------------------------------------

    def _raw(self) -> dict[str, Any]:
        return self.db.get_state()

    def _anchor(self, raw: Mapping[str, Any]) -> datetime:
        """本次推进的起点：上一次真正落库的时间。"""
        for key in ("last_tick", "last_heartbeat", "started_at"):
            parsed = _parse_instant(raw.get(key))
            if parsed is not None:
                return parsed
        return self.now()

    def _baseline(
        self, raw: Mapping[str, Any]
    ) -> tuple[dict[str, float], dict[str, Any]]:
        """从 state 表读出内部字段并兜底成合法值（老库、被手改过的库都能跑）。"""
        numbers = {
            "energy": _clamp(_number(raw.get("energy"), 100.0)),
            "satiety": _clamp(_number(raw.get("satiety"), 80.0)),
            "fatigue": _clamp(_number(raw.get("fatigue"), 20.0)),
            "affection": _clamp(
                _number(raw.get("affection"), AFFECTION_START), AFFECTION_MAX
            ),
        }
        flags = {
            "sleeping": bool(raw.get("sleeping", False)),
            "quiet_override": raw.get("quiet_override"),
            "sleep_requested": bool(raw.get("sleep_requested", False)),
        }
        return numbers, flags

    # -- 纯函数：推进 ----------------------------------------------------

    def _step(
        self,
        numbers: dict[str, float],
        flags: dict[str, Any],
        minutes: float,
    ) -> dict[str, float]:
        """把 ``numbers`` 原地推进 ``minutes`` 分钟。

        睡着时切小段"回升"，醒着时切小段"消耗"；每个小段之后立刻判断是否该
        自动醒 / 自动睡，所以一次 8 小时的推进也不会把醒来后的时间算成睡觉。
        """
        if minutes <= 0:
            return numbers
        remaining = min(minutes, MAX_STEP_MINUTES)
        chunk_size = max(1.0, MAX_STEP_MINUTES / MAX_SEGMENTS)
        while remaining > 1e-9:
            chunk = min(remaining, chunk_size)
            remaining -= chunk
            if flags.get("sleeping"):
                numbers["energy"] = _clamp(
                    numbers["energy"] + chunk * ENERGY_SLEEP_RECOVER_PER_MIN
                )
                numbers["fatigue"] = _clamp(
                    numbers["fatigue"] - chunk * FATIGUE_SLEEP_RECOVER_PER_MIN
                )
                numbers["satiety"] = _clamp(
                    numbers["satiety"] - chunk * SATIETY_SLEEP_DECAY_PER_MIN
                )
            else:
                numbers["energy"] = _clamp(
                    numbers["energy"] - chunk * ENERGY_WAKE_DECAY_PER_MIN
                )
                numbers["fatigue"] = _clamp(
                    numbers["fatigue"] + chunk * FATIGUE_WAKE_GAIN_PER_MIN
                )
                numbers["satiety"] = _clamp(
                    numbers["satiety"] - chunk * SATIETY_DECAY_PER_MIN
                )
                if (
                    numbers["energy"] <= AUTO_SLEEP_ENERGY
                    or numbers["fatigue"] >= AUTO_SLEEP_FATIGUE
                ):
                    flags["sleeping"] = True
        return numbers

    def _quiet_extra_minutes(self, start: datetime, end: datetime) -> float:
        """[start, end) 里落在静默时段内的分钟数（按分钟离散采样，足够精确）。"""
        if not self.quiet_windows or end <= start:
            return 0.0
        span = (end - start).total_seconds() / 60.0
        if span <= 0:
            return 0.0
        span = min(span, MAX_STEP_MINUTES)
        total = 0.0
        for i in range(int(math.ceil(span))):
            moment = start + timedelta(minutes=i + 0.5)
            if any(w.contains(moment.astimezone(self._tz)) for w in self.quiet_windows):
                total += min(1.0, span - i)
        return max(0.0, total)

    def _advance(
        self,
        raw: Mapping[str, Any],
        instant: datetime,
    ) -> tuple[dict[str, float], dict[str, Any], datetime]:
        """把 raw 推进到 instant，返回 (numbers, flags, anchor)。"""
        anchor = self._anchor(raw)
        minutes = max(0.0, (instant - anchor).total_seconds() / 60.0)
        numbers, flags = self._baseline(raw)
        if minutes <= 0:
            return numbers, flags, anchor
        self._step(numbers, flags, minutes)
        if not flags.get("sleeping"):
            quiet_extra = self._quiet_extra_minutes(anchor, instant)
            if quiet_extra > 0:
                numbers["fatigue"] = _clamp(
                    numbers["fatigue"] + quiet_extra * QUIET_FATIGUE_GAIN_PER_MIN
                )
                if numbers["fatigue"] >= AUTO_SLEEP_FATIGUE:
                    flags["sleeping"] = True
        # 冷落度：好感的自然衰减，最多按一天折算
        days = min(minutes, MAX_STEP_MINUTES) / (24 * 60)
        numbers["affection"] = _clamp(
            numbers["affection"] - days * AFFECTION_DECAY_PER_DAY,
            AFFECTION_MAX,
        )
        return numbers, flags, anchor

    def _should_auto_wake(
        self,
        numbers: Mapping[str, float],
        flags: Mapping[str, Any],
        in_quiet: bool,
    ) -> bool:
        """睡够了（能量回满、不困了）就自动醒；静默时段里醒了也没人陪，就接着睡。"""
        if not flags.get("sleeping"):
            return False
        if flags.get("quiet_override") == "sleeping":
            return False
        if flags.get("sleep_requested"):
            # 主人明确让她睡的，只有睡到能量满才自然醒
            return (
                numbers["energy"] >= AUTO_WAKE_ENERGY
                and numbers["fatigue"] <= AUTO_WAKE_FATIGUE
                and not in_quiet
            )
        if in_quiet:
            return False
        return (
            numbers["energy"] >= AUTO_WAKE_ENERGY
            and numbers["fatigue"] <= AUTO_WAKE_FATIGUE
        )

    # -- 心情 / 在场 -----------------------------------------------------

    def _mood(self, numbers: Mapping[str, float], flags: Mapping[str, Any]) -> str:
        energy = numbers["energy"]
        satiety = numbers["satiety"]
        fatigue = numbers["fatigue"]
        if satiety <= STARVING_SATIETY:
            return "饿坏了"
        if flags.get("sleeping"):
            return "平静" if energy >= 40 else "疲惫"
        if energy <= 10 or fatigue >= 85:
            return "疲惫"
        if satiety < HUNGRY_SATIETY:
            return "沮丧"
        if energy <= 40:
            return "有点累"
        if (
            energy >= 80
            and satiety >= 60
            and fatigue <= 40
            and numbers.get("affection", AFFECTION_START) >= 50
        ):
            return "开心"
        return "平静"

    def _presence(
        self,
        numbers: Mapping[str, float],
        flags: Mapping[str, Any],
        quiet: bool,
    ) -> str:
        if flags.get("sleeping"):
            return "睡了"
        if quiet:
            override = flags.get("quiet_override")
            if override == "sleeping":
                return "睡了"
            if override == "awake":
                return "在"
            # 静默时段默认不出声，但人还在
            return "静默"
        if numbers["energy"] <= 20:
            return "打盹"
        return "在"

    @staticmethod
    def _mood_rank(mood: str) -> int:
        return MOODS.index(mood) if mood in MOODS else MOODS.index("平静")

    @staticmethod
    def _presence_reason(
        numbers: Mapping[str, float],
        flags: Mapping[str, Any],
        in_quiet: bool,
        presence: str,
    ) -> str:
        if presence == "睡了":
            if flags.get("sleeping"):
                return "睡着了"
            return "静默时段，默认睡觉"
        if presence == "静默":
            return "处在静默时段，不主动出声"
        if presence == "打盹":
            return f"能量只剩 {_as_int(numbers['energy'])}，在打盹"
        if in_quiet:
            return "静默时段里被主人碰过，可以出声"
        return "正常在场"

    # -- 渲染 ------------------------------------------------------------

    def _render(
        self,
        raw: Mapping[str, Any],
        numbers: Mapping[str, float],
        flags: Mapping[str, Any],
        *,
        instant: datetime,
        anchor: datetime,
    ) -> MoodSnapshot:
        quiet = self.quiet_status(instant)
        override = flags.get("quiet_override")
        # 自动醒来放在渲染里判定：渲染是纯函数，重复推进会得到同一个答案，
        # 不会出现"第一次推进醒了、第二次推进又睡回去"的抖动。
        if self._should_auto_wake(numbers, flags, quiet["in_quiet_hours"]):
            flags = {**flags, "sleeping": False}
        mood = self._mood(numbers, flags)
        presence = self._presence(numbers, flags, quiet["in_quiet_hours"])
        in_quiet = quiet["in_quiet_hours"]
        if override == "awake":
            in_quiet = False
        stamp = instant.isoformat(timespec="milliseconds")

        public: dict[str, Any] = {
            # --- M0 已有字段：只加不改 ---
            "energy": _as_int(numbers["energy"]),
            "mood": mood,
            "presence": presence,
            "sleeping": bool(flags.get("sleeping")),
            "started_at": raw.get("started_at"),
            "last_heartbeat": stamp,
            # --- M1 新增字段 ---
            "satiety": _as_int(numbers["satiety"]),
            "fatigue": _as_int(numbers["fatigue"]),
            "affection": _as_int(numbers["affection"]),
            "mood_rank": self._mood_rank(mood),
            "moods": list(MOODS),
            "presences": list(PRESENCES),
            "presence_reason": self._presence_reason(
                numbers, flags, quiet["in_quiet_hours"], presence
            ),
            "in_quiet_hours": in_quiet,
            "quiet_hours": quiet["quiet_hours"],
            "quiet_windows": quiet["active_windows"],
            "quiet_override": override,
            "local_time": quiet["local_time"],
            "utc_offset_minutes": quiet["utc_offset_minutes"],
            "city": quiet["city"],
            "hungry": numbers["satiety"] < HUNGRY_SATIETY,
            "tired": numbers["fatigue"] >= 70 or numbers["energy"] <= 40,
            "mood_since": raw.get("mood_since") or raw.get("started_at") or stamp,
            "last_interaction": raw.get("last_interaction"),
            "last_client": raw.get("last_client"),
            "last_drag_at": raw.get("last_drag_at"),
            "last_position": raw.get("last_position"),
            "drag_count": int(_number(raw.get("drag_count"), 0)),
            "state_version": 2,
        }
        internal: dict[str, Any] = {
            "energy": round(numbers["energy"], 3),
            "satiety": round(numbers["satiety"], 3),
            "fatigue": round(numbers["fatigue"], 3),
            "affection": round(numbers["affection"], 3),
            "mood": mood,
            "presence": presence,
            "sleeping": bool(flags.get("sleeping")),
            "sleep_requested": bool(flags.get("sleep_requested")),
            "quiet_override": override,
            "last_tick": stamp,
            "last_heartbeat": stamp,
        }
        if raw.get("mood") != mood or not raw.get("mood_since"):
            internal["mood_since"] = stamp

        changed: dict[str, Any] = {
            key: public[key] for key in _TRACKED_FIELDS if raw.get(key) != public[key]
        }
        if anchor is not None and anchor != instant:
            changed["elapsed_minutes"] = round(
                (instant - anchor).total_seconds() / 60.0, 3
            )
        return MoodSnapshot(internal=internal, public=public, changed=changed)

    def _persist(
        self,
        snapshot: MoodSnapshot,
        *,
        force_event: bool = False,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """落库；只有状态真的变了（或调用方要求）才补一条 state.changed。"""
        self.db.set_state(snapshot.internal)
        body = dict(payload or {})
        if snapshot.changed or force_event:
            body.setdefault("changed", snapshot.changed)
            body.setdefault("state", _public_subset(snapshot.public))
            self.db.append_event("灵鲸鲸", STATE_EVENT_KIND, body)
        return snapshot.public

    # -- 公开入口 --------------------------------------------------------

    def heartbeat(
        self,
        now: datetime | None = None,
        *,
        actor: str = "system",
        event_threshold_minutes: float = 1.0,
    ) -> dict[str, Any]:
        """心跳：按流逝的分钟数推进衰减/恢复，必要时写 ``state.changed``。

        这是唯一的"时间驱动"入口，app.py 的后台循环只需要调它。
        ``event_threshold_minutes``：间隔不足这么久、且状态没变化时就不写事件，
        避免每 15 秒一条把事件表刷满（字段照样落库）。
        """
        instant = _normalize_instant(now) if now else self.now()
        raw = self._raw()
        numbers, flags, anchor = self._advance(raw, instant)
        snapshot = self._render(raw, numbers, flags, instant=instant, anchor=anchor)
        elapsed = snapshot.changed.get("elapsed_minutes", 0.0)
        meaningful = len(snapshot.changed) > 1 or elapsed >= event_threshold_minutes
        return self._persist(
            snapshot,
            force_event=meaningful,
            payload={"source": "heartbeat", "actor": actor, "elapsed_minutes": elapsed},
        )

    def tick(self, now: datetime | None = None) -> dict[str, Any]:
        """推进到 ``now`` 并返回最新状态（``GET /v1/state`` 的推荐实现）。

        与 ``heartbeat`` 的区别：tick 是"读之前先对齐时间"，写事件更保守
        （间隔不到 1 分钟且无状态变化不写），避免一次 GET 就制造一堆事件。
        """
        instant = _normalize_instant(now) if now else self.now()
        raw = self._raw()
        numbers, flags, anchor = self._advance(raw, instant)
        snapshot = self._render(raw, numbers, flags, instant=instant, anchor=anchor)
        elapsed = snapshot.changed.get("elapsed_minutes", 0.0)
        # 读状态时也写事件，但太碎（<1 分钟且无字段变化）就不写，免得 GET 刷爆事件表
        if elapsed < 1.0 and len(snapshot.changed) <= 1:
            return self._persist(snapshot)
        # 会话启动时 heartbeat 可能先落过一个"乱纪元"的 last_tick；
        # GET 之后再对齐一次，把这段时间补上（幂等：第二次 elapsed 就是 0）。
        changed_needed = snapshot.changed
        if not changed_needed:
            raw2 = self._raw()
            anchor2 = _parse_instant(raw2.get("last_tick"))
            if anchor2 is not None and anchor2 < instant:
                numbers, flags, _ = self._advance(raw2, instant)
                snapshot = self._render(raw2, numbers, flags, instant=instant, anchor=anchor2)
        return self._persist(snapshot, force_event=True)

    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        """只读预览：推进到 ``now`` 但**不落库**，不产生任何副作用。"""
        instant = _normalize_instant(now) if now else self.now()
        raw = self._raw()
        numbers, flags, anchor = self._advance(raw, instant)
        return self._render(raw, numbers, flags, instant=instant, anchor=anchor).public

    def current_state(self, now: datetime | None = None) -> dict[str, Any]:
        """对外统一入口；永远返回全量字段（老库也能补齐）。"""
        return self.tick(now)

    # -- 睡眠 ------------------------------------------------------------

    def sleep(self, now: datetime | None = None, *, reason: str = "主人让我睡") -> dict[str, Any]:
        instant = _normalize_instant(now) if now else self.now()
        raw = self._raw()
        numbers, flags, _ = self._advance(raw, instant)
        # sleep 是主人的明确意图：即便她已经回满能量，也必须真的睡着
        flags["sleeping"] = True
        flags["sleep_requested"] = True
        flags["quiet_override"] = None
        snapshot = self._render(raw, numbers, flags, instant=instant, anchor=instant)
        return self._persist(
            snapshot, force_event=True, payload={"source": "sleep", "reason": reason}
        )

    def wake(
        self,
        now: datetime | None = None,
        *,
        by: str = "主人",
        quiet_override: str | None = "awake",
    ) -> dict[str, Any]:
        """叫醒。``quiet_override="awake"`` 时，静默时段也会先出声。"""
        instant = _normalize_instant(now) if now else self.now()
        raw = self._raw()
        numbers, flags, _ = self._advance(raw, instant)
        was_sleeping = bool(flags.get("sleeping"))
        flags["sleeping"] = False
        flags["sleep_requested"] = False
        numbers["fatigue"] = _clamp(numbers["fatigue"] - 10.0)
        flags["quiet_override"] = quiet_override
        snapshot = self._render(raw, numbers, flags, instant=instant, anchor=instant)
        return self._persist(
            snapshot,
            force_event=True,
            payload={"source": "wake", "by": by, "was_sleeping": was_sleeping},
        )

    # -- 互动 ------------------------------------------------------------

    def _rate_limited(self, client_id: str, instant: datetime) -> bool:
        window_start = instant - timedelta(seconds=INTERACTION_WINDOW_SECONDS)
        seen = 0
        for event in self.db.list_events(limit=300):
            if event["kind"] != STATE_INTERACTION_KIND:
                continue
            if event["payload"].get("client_id") != client_id:
                continue
            ts = _parse_instant(event["ts"])
            if ts is None or ts < window_start:
                continue
            seen += 1
            if seen >= INTERACTION_WINDOW_LIMIT:
                return True
        return False

    def _wake_chance(self, energy: float) -> float:
        """睡着被拖时晃醒的概率；越累越难叫醒。"""
        return max(0.0, DRAG_WAKE_CHANCE * (1.0 - energy / GAUGE_MAX))

    def interact(
        self,
        kind: str,
        now: datetime | None = None,
        *,
        client_id: str = "desktop",
        detail: Mapping[str, Any] | None = None,
        count: int = 1,
        position: Mapping[str, Any] | None = None,
        rng: Any | None = None,
    ) -> dict[str, Any]:
        """记录一次互动（拖拽/摸摸/投喂/戳/叫醒），立刻改变在场与心情。

        * 拖拽：能量 +0.6、好感 +1.5；睡着时有 ``_wake_chance`` 的概率被晃醒，
          静默时段里被拖一下，presence 会从"静默"变成"在"。
        * ``count`` 允许一次上报多次（前端合批），上限 20 次；另有滑动窗口限流。
        * ``rng`` 可注入（任何带 random() 的对象），测试时确定"醒没醒"。
        """
        kind = (kind or "").strip()
        if kind not in INTERACTION_KINDS:
            raise ValueError(f"未知互动类型：{kind!r}，可用：{', '.join(INTERACTION_KINDS)}")
        instant = _normalize_instant(now) if now else self.now()
        if self._rate_limited(client_id, instant):
            raise ValueError("互动太频繁了，稍等一下再玩")
        raw = self._raw()
        numbers, flags, _ = self._advance(raw, instant)

        energy_delta, affection_delta = INTERACTION_KINDS[kind]
        tries = max(1, min(int(count), 20))
        spoiled = int(_number(raw.get("drag_count"), 0)) >= DRAG_SPOIL_LIMIT

        numbers["energy"] = _clamp(numbers["energy"] + energy_delta * tries)
        affection_gain = 0.5 * affection_delta * tries if spoiled else affection_delta * tries
        numbers["affection"] = _clamp(numbers["affection"] + affection_gain, AFFECTION_MAX)
        numbers["fatigue"] = _clamp(numbers["fatigue"] + INTERACTION_FATIGUE_GAIN * tries)
        if kind == "feed":
            numbers["satiety"] = _clamp(numbers["satiety"] + FEED_SATIETY_GAIN * tries)

        woke_up = False
        if kind in {"drag", "wake"} and flags.get("sleeping"):
            if kind == "wake":
                woke_up = True
            else:
                roll = rng.random() if rng is not None else __import__("random").random()
                woke_up = roll < self._wake_chance(numbers["energy"])
            if woke_up:
                flags["sleeping"] = False
                numbers["energy"] = _clamp(numbers["energy"] + DRAG_WAKE_ENERGY)

        # 静默时段里被主人亲手碰过 = 她可以出声了
        quiet_now = self.quiet_status(instant)["in_quiet_hours"]
        if quiet_now and kind in {"drag", "stroke", "feed", "play", "wake"}:
            flags["quiet_override"] = "awake"

        stamp = instant.isoformat(timespec="milliseconds")
        extra: dict[str, Any] = {
            "last_interaction": stamp,
            "last_client": client_id,
        }
        internal_extra: dict[str, Any] = {
            "last_interaction": stamp,
            "last_client": client_id,
        }
        if kind == "drag":
            extra["last_drag_at"] = stamp
            extra["drag_count"] = int(_number(raw.get("drag_count"), 0)) + tries
            internal_extra["last_drag_at"] = stamp
            internal_extra["drag_count"] = extra["drag_count"]
        if position:
            clean = {k: v for k, v in position.items() if v is not None}
            if clean:
                extra["last_position"] = clean
                internal_extra["last_position"] = clean

        snapshot = self._render(raw, numbers, flags, instant=instant, anchor=instant)
        snapshot.changed.pop("elapsed_minutes", None)
        snapshot.internal.update(internal_extra)
        snapshot.public.update(extra)
        payload = {
            "source": "interaction",
            "kind": kind,
            "client_id": client_id,
            "count": tries,
            "woke_up": woke_up,
            "detail": dict(detail or {}),
        }
        if position:
            payload["position"] = {k: v for k, v in position.items() if v is not None}
        self.db.set_state(snapshot.internal)
        self.db.append_event("主人", STATE_INTERACTION_KIND, payload)
        # 状态真的变了才补一条变更史，免得互动事件和状态事件重复计数
        if snapshot.changed:
            self.db.append_event(
                "灵鲸鲸",
                STATE_EVENT_KIND,
                {
                    "source": f"interaction:{kind}",
                    "changed": snapshot.changed,
                    "state": _public_subset(snapshot.public),
                },
            )
        return snapshot.public

    def drag(
        self,
        now: datetime | None = None,
        *,
        client_id: str = "desktop",
        x: float | None = None,
        y: float | None = None,
        detail: Mapping[str, Any] | None = None,
        rng: Any | None = None,
    ) -> dict[str, Any]:
        """前端拖拽的语义化入口（等价于 ``interact("drag", ...)``）。"""
        position = {"x": x, "y": y} if (x is not None or y is not None) else None
        return self.interact(
            "drag", now, client_id=client_id, detail=detail, position=position, rng=rng
        )

    # -- 静默覆盖 --------------------------------------------------------

    def set_quiet_override(
        self, value: str | None, now: datetime | None = None
    ) -> dict[str, Any]:
        """手动覆盖静默时段：``"awake"`` 强行在场，``"sleeping"`` 强制睡，``None`` 恢复默认。"""
        if value not in (None, "awake", "sleeping"):
            raise ValueError("quiet_override 只能是 None / 'awake' / 'sleeping'")
        instant = _normalize_instant(now) if now else self.now()
        raw = self._raw()
        numbers, flags, _ = self._advance(raw, instant)
        flags["quiet_override"] = value
        if value == "sleeping":
            flags["sleeping"] = True
        elif value == "awake":
            flags["sleeping"] = False
        snapshot = self._render(raw, numbers, flags, instant=instant, anchor=instant)
        return self._persist(
            snapshot,
            force_event=True,
            payload={"source": "quiet_override", "value": value},
        )

    def should_speak(self, now: datetime | None = None) -> bool:
        """任何"由她主动发起"的动作（问候、画画、发信）之前必须问这一句。

        静默时段 / 睡着了 / 饿坏了 / 太累时返回 False ——「静默时段不主动」
        在这里是唯一的真相来源，别的地方不要再自己判断一遍。
        """
        return self.proactive_hint(now)["allowed"]

    def proactive_hint(self, now: datetime | None = None) -> dict[str, Any]:
        """给出"现在能不能主动开口"的判断与理由（M2 的主动行为直接用）。"""
        state = self.snapshot(now)
        reasons: list[str] = []
        if state["in_quiet_hours"]:
            reasons.append("静默时段不主动")
        if state["sleeping"]:
            reasons.append("睡着了不主动")
        if state["mood"] in {"饿坏了", "疲惫"}:
            reasons.append(f"心情是{state['mood']}，先照顾好自己")
        return {"allowed": not reasons, "reasons": reasons, "state": _public_subset(state)}

    # -- 状态变更史 ------------------------------------------------------

    def history(
        self, limit: int = 50, kinds: tuple[str, ...] | None = None
    ) -> list[dict[str, Any]]:
        """最近的状态变更事件（默认只取 ``state.`` 前缀，最新在前）。

        直接复用 append-only 的 ``event`` 表；``/v1/events`` 不受影响。
        """
        prefixes = kinds or (STATE_EVENT_KIND_PREFIX,)
        rows = self.db.list_events(limit=max(limit * 4, 200))
        picked = [
            row
            for row in rows
            if any(row["kind"].startswith(prefix) for prefix in prefixes)
        ]
        return picked[: max(1, limit)]


def _public_subset(state: Mapping[str, Any]) -> dict[str, Any]:
    """事件里带一份精简快照：够回放，又不会把整个 state 塞进日志。"""
    keys = (
        "energy",
        "satiety",
        "fatigue",
        "affection",
        "mood",
        "presence",
        "sleeping",
        "in_quiet_hours",
    )
    return {key: state[key] for key in keys if key in state}


def build_state_machine(db: Database, settings: Any | None = None) -> StateMachine:
    """从 ``Settings`` 构造状态机；没有配置就走默认值（默认静默时段 23:30-07:30）。"""
    if settings is None:
        return StateMachine(db)
    return StateMachine(
        db,
        quiet_hours=getattr(settings, "quiet_hours", None),
        utc_offset_minutes=getattr(
            settings, "state_utc_offset_minutes", DEFAULT_UTC_OFFSET_MINUTES
        ),
        city=getattr(settings, "state_city", DEFAULT_CITY),
    )


STATE_MACHINE_MIN_VERSION = 2


__all__ = [
    "StateMachine",
    "MoodSnapshot",
    "QuietWindow",
    "build_state_machine",
    "parse_quiet_hours",
    "parse_local_time",
    "MOODS",
    "PRESENCES",
    "INTERACTION_KINDS",
    "STATE_EVENT_KIND",
    "STATE_EVENT_KIND_PREFIX",
    "STATE_INTERACTION_KIND",
    "DEFAULT_QUIET_HOURS",
]
