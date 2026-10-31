"""M6 主动话题与打扰预算测试（task-16）。

覆盖任务书要求的：预算上限、静默时段、关闭开关；外加四种话题来源、
事件留痕、以及"没发出去必须说清楚"的不隐藏原则。

约定：时间全部显式传入（aware datetime），测试不睡真实秒数。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from lingbrain.db import Database
from lingbrain.proactive import (
    DEFAULT_DAILY_LIMIT,
    DEFAULT_MIN_GAP_MINUTES,
    ENV_ENABLED,
    EVENT_BLOCKED,
    EVENT_DISABLED,
    EVENT_SENT,
    FallbackGate,
    MemoryRevisitSource,
    ProactiveService,
    ReunionSource,
    ShareQueueSource,
    StateReminderSource,
    Topic,
    build_proactive_service,
    local_day,
    parse_enabled,
)
from lingbrain.proactive_routes import create_proactive_router

CST = timezone(timedelta(hours=8))


def local_noon(now: datetime | None = None) -> datetime:
    """把时刻摆到本地中午（+8 区）。

    * 中午保证前后 6 小时都不会跨本地日，预算测试可以放心在同一"天"内推进。
    * 用"今天"而不是写死日历日期：db 写入的 ts 是真实 utc_now()，
      固定日期会让"历史记录 vs 当前时刻"的比较失真。
    """
    moment = (now or datetime.now(timezone.utc)).astimezone(CST)
    return moment.replace(hour=12, minute=0, second=0, microsecond=0)


#: 预算/节奏类测试的基准时刻：本地中午，非静默时段
T0 = local_noon()
TOKEN = "test-token"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


class FakeGate:
    """可控的闸门：直接模拟 state_machine 的两种返回。"""

    def __init__(self, allowed: bool = True, reasons=None, state=None):
        self.allowed = allowed
        self.reasons = list(reasons or [])
        self.state = dict(state or {})
        self.should_speak_calls = 0
        self.hint_calls = 0

    def should_speak(self, now=None) -> bool:
        self.should_speak_calls += 1
        return self.allowed

    def proactive_hint(self, now=None):
        self.hint_calls += 1
        return {"allowed": self.allowed, "reasons": list(self.reasons), "state": dict(self.state)}


def make_db(tmp_path) -> Database:
    return Database(tmp_path / "proactive.db")


def hungry_gate() -> FakeGate:
    """一个"饿了"的状态：能稳定产出 state_reminder 话题。"""
    return FakeGate(allowed=True, state={"satiety": 5, "in_quiet_hours": False})


def make_service(tmp_path, gate=None, *, daily_limit=DEFAULT_DAILY_LIMIT,
                 min_gap_minutes=DEFAULT_MIN_GAP_MINUTES, enabled=True, sources=None):
    db = make_db(tmp_path)
    gate = gate if gate is not None else hungry_gate()
    if sources is None:
        sources = [StateReminderSource(gate)]
    return ProactiveService(
        db, gate, sources=sources, daily_limit=daily_limit,
        min_gap_minutes=min_gap_minutes, enabled=enabled,
    )


@pytest.fixture
def service(tmp_path) -> ProactiveService:
    svc = make_service(tmp_path)
    yield svc
    svc.db.close()


def kinds(svc: ProactiveService) -> list[str]:
    return [item["kind"] for item in svc.history(limit=100)]


# ---------------------------------------------------------------------------
# 1. 打扰预算：每天 ≤3 次
# ---------------------------------------------------------------------------


def test_daily_limit_defaults_to_three():
    assert DEFAULT_DAILY_LIMIT == 3


def test_budget_allows_exactly_three_per_day(service):
    sent = 0
    for i in range(5):
        moment = T0 + timedelta(minutes=i * DEFAULT_MIN_GAP_MINUTES + i)
        result = service.tick(moment)
        if result["sent"]:
            sent += 1
    assert sent == 3
    assert service.sent_today(T0 + timedelta(hours=6)) == 3


def test_fourth_attempt_is_blocked_with_reason(service):
    for i in range(3):
        assert service.tick(T0 + timedelta(minutes=i * (DEFAULT_MIN_GAP_MINUTES + 1)))["sent"] is True
    result = service.tick(T0 + timedelta(minutes=400))
    assert result["sent"] is False
    assert any("上限" in reason for reason in result["blocked_reasons"])


def test_budget_resets_next_local_day(service):
    for i in range(3):
        service.tick(T0 + timedelta(minutes=i * (DEFAULT_MIN_GAP_MINUTES + 1)))
    assert service.sent_today(T0) == 3
    tomorrow = T0 + timedelta(days=1)
    assert service.sent_today(tomorrow) == 0
    assert service.tick(tomorrow)["sent"] is True


def test_blocked_attempts_do_not_consume_budget(service):
    # 静默时段被拦下三次，额度应该还是满的
    service.gate = FakeGate(allowed=False, reasons=["静默时段不主动"])
    for i in range(3):
        service.tick(T0 + timedelta(minutes=i))
    assert service.sent_today(T0) == 0
    # 恢复可说话后额度没有被消耗
    service.gate = hungry_gate()
    assert service.tick(T0 + timedelta(minutes=10))["sent"] is True


def test_min_gap_between_messages(service):
    assert service.tick(T0)["sent"] is True
    too_soon = service.tick(T0 + timedelta(minutes=5))
    assert too_soon["sent"] is False
    assert any("不足" in reason for reason in too_soon["blocked_reasons"])
    # 过了间隔就行
    assert service.tick(T0 + timedelta(minutes=DEFAULT_MIN_GAP_MINUTES + 1))["sent"] is True


def test_status_reports_used_remaining_and_next_available(service):
    status = service.status(T0)
    assert status["used_today"] == 0
    assert status["remaining"] == 3
    assert status["daily_limit"] == 3

    service.tick(T0)
    status = service.status(T0 + timedelta(minutes=1))
    assert status["used_today"] == 1
    assert status["remaining"] == 2
    assert status["next_available_at"] is not None


def test_custom_daily_limit_zero_never_speaks(tmp_path):
    svc = make_service(tmp_path, daily_limit=0)
    result = svc.tick(T0)
    assert result["sent"] is False


# ---------------------------------------------------------------------------
# 2. 静默时段：复用 should_speak / proactive_hint
# ---------------------------------------------------------------------------


def test_quiet_hours_blocks_and_explains(tmp_path):
    gate = FakeGate(allowed=False, reasons=["静默时段不主动"], state={"in_quiet_hours": True})
    svc = make_service(tmp_path, gate)
    result = svc.tick(T0)
    assert result["sent"] is False
    assert "静默时段不主动" in result["blocked_reasons"]
    assert svc.sent_today(T0) == 0


def test_sleeping_blocks(tmp_path):
    gate = FakeGate(allowed=False, reasons=["睡着了不主动"], state={"sleeping": True})
    svc = make_service(tmp_path, gate)
    result = svc.tick(T0)
    assert result["sent"] is False
    assert any("睡着了" in r for r in result["blocked_reasons"])


def test_gate_is_actually_consulted(service):
    service.tick(T0)
    # 每次判断都要问状态机，不能自己缓存一个"现在能说"
    assert service.gate.hint_calls >= 1
    assert service.gate.should_speak_calls >= 1


def test_should_speak_cross_check_when_hint_says_allowed(tmp_path):
    """hint 允许但 should_speak() 说不允许时，保守地不发。"""

    class InconsistentGate(FakeGate):
        def should_speak(self, now=None):
            return False

    gate = InconsistentGate(allowed=True, state={"satiety": 5})
    svc = make_service(tmp_path, gate)
    result = svc.tick(T0)
    assert result["sent"] is False
    assert any("should_speak" in r for r in result["blocked_reasons"])


def test_broken_gate_means_never_speak(tmp_path):
    class BrokenGate:
        def should_speak(self, now=None):
            raise RuntimeError("状态机炸了")

        def proactive_hint(self, now=None):
            raise RuntimeError("状态机炸了")

    svc = make_service(tmp_path, BrokenGate())
    result = svc.tick(T0)
    assert result["sent"] is False
    assert result["blocked_reasons"]


def test_fallback_gate_is_conservative():
    """没有状态机时默认不主动，并把原因写清楚。"""
    gate = FallbackGate()
    assert gate.should_speak(T0) is False
    hint = gate.proactive_hint(T0)
    assert hint["allowed"] is False
    assert hint["reasons"]


# ---------------------------------------------------------------------------
# 3. 关闭开关
# ---------------------------------------------------------------------------


def test_disabled_never_sends(tmp_path):
    svc = make_service(tmp_path, enabled=False)
    result = svc.tick(T0)
    assert result["sent"] is False
    assert result["enabled"] is False
    assert any("关闭" in r for r in result["blocked_reasons"])
    assert svc.sent_today(T0) == 0


def test_disabled_is_logged_not_silent(tmp_path):
    svc = make_service(tmp_path, enabled=False)
    svc.tick(T0)
    assert EVENT_DISABLED in kinds(svc)


def test_status_shows_disabled(tmp_path):
    svc = make_service(tmp_path, enabled=False)
    status = svc.status(T0)
    assert status["enabled"] is False
    assert status["allowed"] is False


def test_parse_enabled_values():
    assert parse_enabled(None, default=True) is True
    assert parse_enabled("1") is True
    assert parse_enabled("true") is True
    assert parse_enabled("0") is False
    assert parse_enabled("false") is False
    assert parse_enabled("off") is False
    assert parse_enabled(False) is False
    assert parse_enabled(True) is True


def test_env_flag_disables_service(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_ENABLED, "0")
    db = make_db(tmp_path)
    svc = build_proactive_service(db, hungry_gate())
    assert svc.enabled is False
    assert svc.tick(T0)["sent"] is False


def test_env_flag_can_enable(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_ENABLED, "1")
    db = make_db(tmp_path)
    svc = build_proactive_service(db, hungry_gate())
    assert svc.enabled is True
    assert svc.tick(T0)["sent"] is True


# ---------------------------------------------------------------------------
# 4. 话题来源
# ---------------------------------------------------------------------------


def test_state_reminder_source_for_hunger(tmp_path):
    gate = FakeGate(allowed=True, state={"satiety": 3})
    topic = StateReminderSource(gate).propose(T0)
    assert topic is not None
    assert topic.kind == "state_reminder"
    assert "饿" in topic.text
    assert topic.evidence_id is None


def test_state_reminder_source_for_fatigue(tmp_path):
    gate = FakeGate(allowed=True, state={"fatigue": 90, "satiety": 80})
    topic = StateReminderSource(gate).propose(T0)
    assert topic is not None
    assert "困" in topic.text


def test_state_reminder_returns_none_when_all_fine():
    gate = FakeGate(allowed=True, state={"satiety": 80, "fatigue": 20, "energy": 90, "mood": "平静"})
    assert StateReminderSource(gate).propose(T0) is None


def test_memory_revisit_uses_real_memory(tmp_path):
    db = make_db(tmp_path)
    memory = db.add_memory("preference", "主人", "主人喜欢蓝鲸", 0.9, "test", None)
    source = MemoryRevisitSource(db, revisit_days=0.0)
    # 记忆的 ts 是真实 utc_now()，所以回访时刻必须取它在之后
    topic = source.propose(datetime.now(timezone.utc))
    assert topic is not None
    assert topic.kind == "memory_revisit"
    assert "蓝鲸" in topic.text
    # 引用了真实记忆 id，可追溯
    assert topic.evidence_id == memory["id"]


def test_memory_revisit_does_not_repeat_same_memory(tmp_path):
    db = make_db(tmp_path)
    db.add_memory("preference", "主人", "主人喜欢蓝鲸", 0.9, "test", None)
    svc = ProactiveService(
        db, hungry_gate(), sources=[MemoryRevisitSource(db, revisit_days=0.0)],
        min_gap_minutes=0, daily_limit=5,
    )
    moment = datetime.now(timezone.utc)
    first = svc.tick(moment)
    second = svc.tick(moment + timedelta(minutes=1))
    assert first["sent"] is True
    # 同一条记忆不会被立刻再拿出来
    assert second["sent"] is False


def test_memory_revisit_without_memory_returns_none(tmp_path):
    db = make_db(tmp_path)
    assert MemoryRevisitSource(db).propose(T0) is None


def test_reunion_source_fires_after_long_silence(tmp_path):
    db = make_db(tmp_path)
    db.append_event("主人", "user.message", {"content": "我出门了"})
    # 事件的 ts 是真实 utc_now()；阈值设 0 表示"只要有过消息就算久别"，
    # 再取一个在它之后的时刻来判定。
    source = ReunionSource(db, reunion_hours=0.0)
    topic = source.propose(datetime.now(timezone.utc) + timedelta(seconds=1))
    assert topic is not None
    assert topic.kind == "reunion"


def test_reunion_source_quiet_without_previous_message(tmp_path):
    db = make_db(tmp_path)
    assert ReunionSource(db).propose(T0) is None


def test_share_queue_source_without_service_returns_none():
    assert ShareQueueSource(None).propose(T0) is None


def test_share_queue_source_reads_queued_items():
    class FakeShare:
        def queued_items(self, now=None):
            return [{"title": "一篇讲鲸落的文章", "key": "k1"}]

    topic = ShareQueueSource(FakeShare()).propose(T0)
    assert topic is not None
    assert topic.kind == "share_queue"
    assert "鲸落" in topic.text


def test_no_topic_means_no_message(tmp_path):
    """没有可用话题时坚决不发，不硬凑。"""
    gate = FakeGate(allowed=True, state={"satiety": 80, "fatigue": 10, "energy": 90})
    svc = ProactiveService(make_db(tmp_path), gate, sources=[StateReminderSource(gate)])
    result = svc.tick(T0)
    assert result["sent"] is False
    assert any("没有" in r for r in result["blocked_reasons"])


def test_broken_source_does_not_crash(tmp_path):
    class ExplodingSource:
        kind = "exploding"

        def propose(self, now=None):
            raise RuntimeError("来源坏了")

    svc = ProactiveService(
        make_db(tmp_path), hungry_gate(),
        sources=[ExplodingSource(), StateReminderSource(hungry_gate())],
    )
    # 坏来源被跳过，后面正常的来源仍然能出话题
    result = svc.tick(T0)
    assert result["sent"] is True
    assert result["topic"]["kind"] == "state_reminder"


# ---------------------------------------------------------------------------
# 5. 事件留痕
# ---------------------------------------------------------------------------


def test_sent_event_is_written(service):
    result = service.tick(T0)
    assert result["sent"] is True
    sent = [e for e in service.history(50) if e["kind"] == EVENT_SENT]
    assert sent, "主动发出必须写 proactive.sent 事件"
    payload = sent[0]["payload"]
    assert payload["topic"] == "state_reminder"
    assert payload["text"]
    assert payload["source"]
    assert payload["day"]


def test_blocked_event_is_written(tmp_path):
    gate = FakeGate(allowed=False, reasons=["静默时段不主动"])
    svc = make_service(tmp_path, gate)
    svc.tick(T0)
    assert EVENT_BLOCKED in kinds(svc)


def test_history_only_returns_proactive_events(service):
    service.db.append_event("主人", "user.message", {"content": "你好"})
    service.tick(T0)
    assert all(e["kind"].startswith("proactive.") for e in service.history(50))


def test_long_text_is_truncated(service):
    class LongTopicSource:
        kind = "long"

        def propose(self, now=None):
            return Topic(kind="long", text="啊" * 500, source="测试")

    service.sources = [LongTopicSource()]
    result = service.tick(T0)
    assert result["sent"] is True
    assert len(result["topic"]["text"]) <= 200


# ---------------------------------------------------------------------------
# 6. HTTP 路由
# ---------------------------------------------------------------------------


def build_app(tmp_path, gate=None, *, enabled=True):
    db = make_db(tmp_path)
    gate = gate if gate is not None else hungry_gate()
    svc = ProactiveService(
        db, gate, sources=[StateReminderSource(gate)],
        enabled=enabled, min_gap_minutes=0,
    )
    app = FastAPI()

    async def authorized(x_ling_token: str = Header(default="")) -> None:
        if x_ling_token != TOKEN:
            raise HTTPException(status_code=401, detail="认证失败")

    app.include_router(create_proactive_router(svc, authorize=authorized))
    return app, svc


def headers() -> dict[str, str]:
    return {"X-Ling-Token": TOKEN}


def test_routes_require_auth(tmp_path):
    app, _ = build_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/v1/proactive/status").status_code == 401
        assert client.post("/v1/proactive/tick").status_code == 401


def test_status_route(tmp_path):
    app, _ = build_app(tmp_path)
    with TestClient(app) as client:
        body = client.get("/v1/proactive/status", headers=headers()).json()
        assert body["enabled"] is True
        assert body["daily_limit"] == 3
        assert body["used_today"] == 0
        assert body["remaining"] == 3


def test_tick_route_sends_and_reports(tmp_path):
    app, _ = build_app(tmp_path)
    with TestClient(app) as client:
        body = client.post("/v1/proactive/tick", headers=headers()).json()
        assert body["sent"] is True
        assert body["topic"]["kind"] == "state_reminder"
        assert body["budget"]["used"] == 1


def test_tick_route_reports_blocked_instead_of_hiding(tmp_path):
    app, _ = build_app(tmp_path, enabled=False)
    with TestClient(app) as client:
        response = client.post("/v1/proactive/tick", headers=headers())
        assert response.status_code == 200
        body = response.json()
        assert body["sent"] is False
        assert body["blocked_reasons"]


def test_history_route(tmp_path):
    app, _ = build_app(tmp_path)
    with TestClient(app) as client:
        client.post("/v1/proactive/tick", headers=headers())
        items = client.get("/v1/proactive/history", headers=headers()).json()["items"]
        assert items
        assert items[0]["kind"].startswith("proactive.")


def test_local_day_uses_configured_offset():
    moment = datetime(2026, 10, 31, 17, 0, tzinfo=timezone.utc)  # UTC 17:00 = 北京次日 01:00
    assert local_day(moment, offset_minutes=480) == "2026-11-01"
    assert local_day(moment, offset_minutes=0) == "2026-10-31"
