"""M1 状态机测试（task-4）。

覆盖：能量随时间衰减、睡眠恢复、拖拽改变 presence、静默时段不主动、
/v1/state 向后兼容、状态变更史接口。

约定：所有时间都是显式传入的 aware datetime，测试不睡真实秒数。
默认参数：UTC+8，静默时段 23:30-07:30。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI, Header
from fastapi.testclient import TestClient

from lingbrain.app import create_app
from lingbrain.config import Settings
from lingbrain.db import Database
from lingbrain.schemas import InteractionIn
from lingbrain.state_machine import (
    AUTO_SLEEP_ENERGY,
    DEFAULT_QUIET_HOURS,
    ENERGY_WAKE_DECAY_PER_MIN,
    MOODS,
    StateMachine,
    build_state_machine,
    parse_quiet_hours,
)
from lingbrain.state_routes import bind_state_routes, create_state_router

TOKEN = "test-token"
CST = timezone(timedelta(hours=8))  # 本机没有 tzdata，直接用固定偏移
T0 = datetime(2026, 10, 31, 14, 0, 0, tzinfo=CST)  # 周六下午 2 点：非静默时段


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


class FixedRng:
    """可预测的 rng：random() 永远返回 0 → 拖拽一定把她晃醒。"""

    def __init__(self, value: float) -> None:
        self.value = value

    def random(self) -> float:
        return self.value


def make_db(tmp_path) -> Database:
    db = Database(tmp_path / "state.db")
    db.set_state({"last_tick": T0.isoformat(timespec="milliseconds")})
    return db


def make_machine(tmp_path, **kwargs) -> StateMachine:
    machine = StateMachine(make_db(tmp_path), **kwargs)
    machine.db.set_state({"last_tick": T0.isoformat(timespec="milliseconds")})
    return machine


@pytest.fixture
def machine(tmp_path) -> StateMachine:
    m = make_machine(tmp_path)
    yield m
    m.db.close()


def quiet_moment() -> datetime:
    """凌晨 2 点（本地），必在 23:30-07:30 的静默时段里。"""
    return datetime(2026, 11, 1, 2, 0, 0, tzinfo=CST)


# ---------------------------------------------------------------------------
# 1. 时间衰减
# ---------------------------------------------------------------------------


def test_energy_decays_with_elapsed_time(machine):
    later = T0 + timedelta(minutes=60)
    state = machine.heartbeat(later)
    expected = 100 - 60 * ENERGY_WAKE_DECAY_PER_MIN
    assert state["energy"] == round(expected)
    assert state["energy"] < 100
    # 时间推进、心跳推进都记进 state 表
    assert machine.db.get_state()["last_tick"] is not None


def test_energy_decay_is_proportional_to_elapsed_time(machine):
    one_hour = machine.snapshot(T0 + timedelta(minutes=60))["energy"]
    two_hours = machine.snapshot(T0 + timedelta(minutes=120))["energy"]
    assert one_hour == 88
    assert two_hours == 76


def test_already_consumed_time_is_not_counted_twice(machine):
    first = machine.heartbeat(T0 + timedelta(minutes=30))
    # 同一个时间点再心跳一次，不应该继续掉能量
    second = machine.heartbeat(T0 + timedelta(minutes=30))
    assert first["energy"] == second["energy"] == 94


def test_energy_drops_into_tired_moods(machine):
    # 5 小时 = 掉 60 点 → 40，开始"有点累"
    state = machine.heartbeat(T0 + timedelta(hours=5))
    assert state["energy"] <= 40  # 100 - 5*60*0.2 = 40
    assert state["mood"] in {"有点累", "疲惫"}
    assert state["tired"] is True


def test_long_gap_is_capped_and_never_negative(machine):
    state = machine.heartbeat(T0 + timedelta(days=3))
    assert state["energy"] >= 0
    assert state["presence"] in {"睡了", "打盹", "静默", "在"}


def test_clock_going_backwards_does_not_reverse_decay(machine):
    machine.heartbeat(T0 + timedelta(minutes=30))
    state = machine.heartbeat(T0)  # 系统时间被往回拨
    assert state["energy"] == 94


def test_satiety_declines_and_sets_hungry_flag(machine):
    state = machine.heartbeat(T0 + timedelta(hours=12))
    assert state["satiety"] < 80
    assert "hungry" in state


# ---------------------------------------------------------------------------
# 2. 睡眠 / 自动入睡 / 恢复
# ---------------------------------------------------------------------------


def test_goes_to_sleep_when_exhausted_and_recovers(machine):
    # 7 小时 → 能量掉到 16；再撑一会儿跌破 12 就自动睡
    tired = machine.heartbeat(T0 + timedelta(minutes=450))  # 7.5h → 10
    assert tired["sleeping"] is True
    assert tired["presence"] == "睡了"

    # 从 22 的能量开始睡：+1/分钟，需要 73 分钟才到 95（自动醒阈值）
    base = T0 + timedelta(minutes=450)
    machine.sleep(base)
    napped = machine.heartbeat(base + timedelta(minutes=30))
    assert napped["energy"] == round(22 + 30 * 1.0)  # 52
    assert napped["sleeping"] is True  # 还没睡够

    recovered = machine.heartbeat(base + timedelta(minutes=80))
    assert recovered["energy"] >= 95
    assert recovered["sleeping"] is False


def test_sleep_recovers_faster_than_waking_drains(tmp_path):
    machine = make_machine(tmp_path)
    machine.db.set_state({"energy": 50.0, "fatigue": 10.0, "last_tick": T0.isoformat()})
    machine.sleep(T0)
    slept = machine.heartbeat(T0 + timedelta(minutes=60))
    assert slept["energy"] == 100  # 50 + 60*1.0，回满并自动醒
    assert slept["sleeping"] is False

    # 同样的一小时如果醒着：50 - 12 = 38
    other = make_machine(tmp_path)
    other.db.set_state({"energy": 50.0, "fatigue": 10.0, "last_tick": T0.isoformat()})
    awake = other.heartbeat(T0 + timedelta(minutes=60))
    assert awake["energy"] < 40


def test_explicit_sleep_and_wake_roundtrip(machine):
    slept = machine.sleep(T0)
    assert slept["sleeping"] is True
    assert slept["presence"] == "睡了"

    woke = machine.wake(T0 + timedelta(minutes=1))
    assert woke["sleeping"] is False
    assert woke["presence"] in {"在", "打盹"}


def test_auto_sleep_threshold_reached_from_low_energy(tmp_path):
    m = make_machine(tmp_path)
    m.db.set_state({"energy": AUTO_SLEEP_ENERGY + 1, "fatigue": 10})
    state = m.heartbeat(T0 + timedelta(minutes=30))
    assert state["sleeping"] is True
    assert state["presence"] == "睡了"
    assert state["mood"] in MOODS

    # 睡够以后醒来
    state = m.heartbeat(T0 + timedelta(minutes=30 + 24 * 60))
    assert state["sleeping"] is False


# ---------------------------------------------------------------------------
# 3. 拖拽 / 互动改变 presence
# ---------------------------------------------------------------------------


def test_drag_changes_presence_and_affection(machine):
    before = machine.snapshot(T0)
    after = machine.drag(T0 + timedelta(minutes=1), client_id="desk", x=10, y=20)
    assert after["presence"] != "静默"
    assert after["last_drag_at"] is not None
    assert after["drag_count"] == 1
    assert after["affection"] >= before["affection"]
    assert after["last_client"] == "desk"
    assert after["last_position"] == {"x": 10.0, "y": 20.0}


def test_drag_wakes_a_sleeping_pet(machine):
    machine.sleep(T0)
    state = machine.drag(T0 + timedelta(minutes=30), rng=FixedRng(0.0))
    assert state["sleeping"] is False
    assert state["presence"] != "睡了"


def test_drag_can_fail_to_wake_when_deeply_asleep(machine):
    machine.db.set_state({"energy": 90, "fatigue": 10})
    machine.sleep(T0)
    state = machine.drag(T0 + timedelta(minutes=1), rng=FixedRng(0.99))
    assert state["sleeping"] is True
    assert state["presence"] == "睡了"


def test_drag_in_quiet_hours_makes_presence_present(machine):
    instant = quiet_moment()
    # 先让她醒着（默认参数下凌晨会困到睡着）
    machine.wake(instant, quiet_override=None)
    sleepy = machine.snapshot(instant)
    assert sleepy["in_quiet_hours"] is True
    assert sleepy["presence"] == "静默"

    awake = machine.drag(instant, rng=FixedRng(0.99))
    assert awake["presence"] == "在"
    # 被亲手碰过 → 静默覆盖为 awake，时段本身没变
    assert awake["quiet_override"] == "awake"
    assert awake["quiet_hours"] == DEFAULT_QUIET_HOURS


def test_feed_increases_satiety(machine):
    machine.db.set_state({"satiety": 10})
    state = machine.interact("feed", T0 + timedelta(minutes=1))
    assert state["satiety"] > 10
    assert state["hungry"] is False


def test_unknown_interaction_is_rejected(machine):
    with pytest.raises(ValueError):
        machine.interact("挠痒痒", T0)


def test_interaction_is_rate_limited(machine):
    client = "spammy"
    for i in range(60):
        machine.interact("drag", T0 + timedelta(seconds=i), client_id=client)
    with pytest.raises(ValueError):
        machine.interact("drag", T0 + timedelta(seconds=61), client_id=client)


# ---------------------------------------------------------------------------
# 4. 静默时段不主动
# ---------------------------------------------------------------------------


def test_quiet_hours_parsing():
    windows = parse_quiet_hours(DEFAULT_QUIET_HOURS)
    assert len(windows) == 1
    assert windows[0].as_dict() == {"start": "23:30", "end": "07:30"}

    multi = parse_quiet_hours("23:30-07:30,13:00-14:00")
    assert len(multi) == 2

    with pytest.raises(ValueError):
        parse_quiet_hours("23:30~")  # 缺结束时间
    with pytest.raises(ValueError):
        parse_quiet_hours("25:00-07:30")


def test_quiet_window_handles_midnight_wrap(tmp_path):
    machine = make_machine(tmp_path)
    assert machine.in_quiet_hours(datetime(2026, 11, 1, 2, 0, tzinfo=CST)) is True
    assert machine.in_quiet_hours(datetime(2026, 11, 1, 12, 0, tzinfo=CST)) is False
    assert machine.in_quiet_hours(datetime(2026, 11, 1, 23, 45, tzinfo=CST)) is True
    assert machine.in_quiet_hours(datetime(2026, 11, 1, 23, 29, tzinfo=CST)) is False
    assert machine.in_quiet_hours(datetime(2026, 11, 1, 7, 30, tzinfo=CST)) is False


def test_no_proactive_action_during_quiet_hours(machine):
    instant = quiet_moment()
    hint = machine.proactive_hint(instant)
    assert hint["allowed"] is False
    assert any("静默" in reason for reason in hint["reasons"])
    assert machine.should_speak(instant) is False
    assert machine.snapshot(instant)["in_quiet_hours"] is True


def test_proactive_action_allowed_during_daytime(machine):
    hint = machine.proactive_hint(T0)
    assert hint["allowed"] is True, hint["reasons"]
    assert hint["reasons"] == []
    assert machine.should_speak(T0) is True


def test_sleeping_pet_does_not_initiate(machine):
    machine.sleep(T0)
    assert machine.should_speak(T0 + timedelta(minutes=1)) is False


def test_quiet_override_restores_proactive(machine):
    instant = quiet_moment()
    assert machine.should_speak(instant) is False
    state = machine.set_quiet_override("awake", instant)
    assert state["presence"] == "在"
    assert machine.should_speak(instant) is True

    machine.set_quiet_override(None, instant)
    assert machine.should_speak(instant) is False


def test_quiet_hours_are_configurable(tmp_path):
    machine = make_machine(tmp_path, quiet_hours="")
    assert machine.in_quiet_hours(quiet_moment()) is False
    machine.wake(T0, quiet_override=None)
    assert machine.should_speak(T0) is True  # 白天醒着，可以主动


def test_quiet_hours_follow_local_offset(tmp_path):
    m = make_machine(tmp_path, utc_offset_minutes=0, quiet_hours="23:30-07:30")
    # 偏移为 0 时，本地时间就是 UTC：19:00 不静默，23:45 静默
    assert m.in_quiet_hours(datetime(2026, 11, 1, 19, 0, tzinfo=timezone.utc)) is False
    assert m.in_quiet_hours(datetime(2026, 11, 1, 23, 45, tzinfo=timezone.utc)) is True

    cst = make_machine(tmp_path, utc_offset_minutes=480, quiet_hours="23:30-07:30")
    # 同一个 UTC 时刻：+8 区已经是次日 03:00，处在静默时段
    assert cst.in_quiet_hours(datetime(2026, 11, 1, 19, 0, tzinfo=timezone.utc)) is True


# ---------------------------------------------------------------------------
# 5. 状态变更史
# ---------------------------------------------------------------------------


def test_history_records_changes(machine):
    machine.heartbeat(T0 + timedelta(minutes=60))
    machine.drag(T0 + timedelta(minutes=61))
    history = machine.history(limit=20)
    assert history, "状态变化后应该有变更史"
    kinds = {item["kind"] for item in history}
    assert "state.changed" in kinds
    assert "state.interaction" in kinds
    changes = [item for item in history if item["kind"] == "state.changed"]
    assert changes
    heartbeat_entry = next(
        item for item in changes if item["payload"].get("source") == "heartbeat"
    )
    assert heartbeat_entry["payload"]["changed"]["energy"] == 88
    assert heartbeat_entry["payload"]["elapsed_minutes"] == 60.0
    assert heartbeat_entry["payload"]["state"]["mood"] in MOODS


def test_history_respects_limit(machine):
    for minute in range(10):
        machine.heartbeat(T0 + timedelta(minutes=minute * 5))
    assert len(machine.history(limit=3)) == 3


# ---------------------------------------------------------------------------
# 6. /v1/state 向后兼容（只加字段），以及新接口
# ---------------------------------------------------------------------------


M0_STATE_FIELDS = {"energy", "mood", "presence", "sleeping", "started_at", "last_heartbeat"}


def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        auth_token=TOKEN,
        model_provider="echo",
        heartbeat_seconds=3600,
    )


def headers() -> dict[str, str]:
    return {"X-Ling-Token": TOKEN}


def build_wired_app(tmp_path) -> FastAPI:
    """按 Lead 的接线方式，把状态路由挂到真实的 create_app 上（不改 app.py 本身）。"""
    app = create_app(settings(tmp_path))

    async def authorize(x_ling_token: str = Header(default="")) -> None:
        if x_ling_token != TOKEN:
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="认证失败")

    machine = build_state_machine(app.state.db, settings(tmp_path))
    bind_state_routes(app, machine, authorize=authorize)
    app.include_router(create_state_router(machine, authorize=authorize))
    return app


def test_existing_state_endpoint_only_gains_fields(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        baseline = client.get("/v1/state", headers=headers())
        assert baseline.status_code == 200
        m0_keys = set(baseline.json())

    assert M0_STATE_FIELDS <= m0_keys

    with TestClient(build_wired_app(tmp_path / "wired")) as client:
        enriched = client.get("/v1/state", headers=headers())
        assert enriched.status_code == 200
        body = enriched.json()
        # 老字段一个不少，且类型不变
        assert M0_STATE_FIELDS <= set(body)
        assert isinstance(body["energy"], int)
        assert 0 <= body["energy"] <= 100
        assert isinstance(body["mood"], str)
        assert isinstance(body["presence"], str)
        assert isinstance(body["sleeping"], bool)
        # 新增字段按契约出现
        for key in ("satiety", "fatigue", "affection", "mood_rank", "in_quiet_hours",
                    "quiet_hours", "presence_reason", "local_time", "state_version"):
            assert key in body, key
        assert body["state_version"] == 2
        assert body["mood"] in MOODS


def test_state_endpoint_still_requires_auth(tmp_path):
    with TestClient(build_wired_app(tmp_path)) as client:
        assert client.get("/v1/state").status_code == 401
        assert client.get("/v1/state/events").status_code == 401
        assert client.post("/v1/state/interact", json={"kind": "drag"}).status_code == 401


def test_state_endpoint_advances_time(tmp_path):
    app = build_wired_app(tmp_path)
    machine: StateMachine = app.state.machine
    instant = machine.now()
    machine.db.set_state({"last_tick": (instant - timedelta(hours=2)).isoformat()})
    with TestClient(app) as client:
        body = client.get("/v1/state", headers=headers()).json()
    assert body["energy"] < 100


def test_state_events_endpoint(tmp_path):
    app = build_wired_app(tmp_path)
    machine: StateMachine = app.state.machine
    instant = machine.now()
    machine.heartbeat(instant + timedelta(minutes=60))
    machine.drag(instant + timedelta(minutes=61), client_id="shell")

    with TestClient(app) as client:
        response = client.get("/v1/state/events", headers=headers(), params={"limit": 10})
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert items
        kinds = {item["kind"] for item in items}
        assert kinds <= {"state.changed", "state.interaction"}
        assert all(item["ts"] for item in items)

        only_interactions = client.get(
            "/v1/state/events", headers=headers(), params={"kinds": "state.interaction"}
        ).json()["items"]
        assert {item["kind"] for item in only_interactions} == {"state.interaction"}


def test_interact_endpoint_changes_presence(tmp_path):
    app = build_wired_app(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/v1/state/interact",
            headers=headers(),
            json={"kind": "drag", "client_id": "shell", "x": 5, "y": 6},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["drag_count"] >= 1
        assert body["last_position"] == {"x": 5.0, "y": 6.0}
        assert body["presence"] != "静默"


def test_sleep_wake_and_quiet_endpoints(tmp_path):
    app = build_wired_app(tmp_path)
    with TestClient(app) as client:
        assert client.post("/v1/state/sleep", headers=headers()).json()["sleeping"] is True
        assert client.post("/v1/state/wake", headers=headers()).json()["sleeping"] is False
        quiet = client.post("/v1/state/quiet", headers=headers(), json={"value": "sleeping"})
        assert quiet.status_code == 200, quiet.text
        assert quiet.json()["sleeping"] is True
        restored = client.post("/v1/state/quiet", headers=headers(), json={})
        assert restored.status_code == 200


def test_proactive_endpoint(tmp_path):
    app = build_wired_app(tmp_path)
    with TestClient(app) as client:
        body = client.get("/v1/state/proactive", headers=headers()).json()
        assert isinstance(body["allowed"], bool)
        assert "state" in body


def test_interact_endpoint_rejects_unknown_kind(tmp_path):
    with TestClient(build_wired_app(tmp_path)) as client:
        response = client.post(
            "/v1/state/interact", headers=headers(), json={"kind": "挠痒痒"}
        )
        assert response.status_code == 422


def test_existing_chat_and_events_endpoints_unaffected(tmp_path):
    """接线后 M0 的 /v1/chat、/v1/events、/v1/health 仍然是原样。"""
    with TestClient(build_wired_app(tmp_path)) as client:
        chat = client.post(
            "/v1/chat", headers=headers(), json={"message": "你好", "client_id": "test"}
        )
        assert chat.status_code == 200, chat.text
        assert M0_STATE_FIELDS <= set(chat.json()["state"])

        events = client.get("/v1/events", headers=headers()).json()["items"]
        assert {"user.message", "assistant.message"} <= {item["kind"] for item in events}
        assert client.get("/v1/health", headers=headers()).status_code == 200


def test_build_state_machine_defaults_from_settings():
    settings_obj = Settings(data_dir=Path("."))
    assert settings_obj.quiet_hours == DEFAULT_QUIET_HOURS
    assert settings_obj.state_utc_offset_minutes == 480
    # 新的配置项都是"只加字段"：老代码 setattr 不受影响
    machine = build_state_machine(__import__("lingbrain.db", fromlist=["Database"]).Database(
        Path(".") / "noop.db"
    ), settings_obj)
    assert machine.quiet_spec == DEFAULT_QUIET_HOURS


def test_interaction_schema_defaults():
    request = InteractionIn(kind="stroke")
    assert request.count == 1
    assert request.client_id == "desktop"
