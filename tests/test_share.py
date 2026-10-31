"""task-11 M4 分享队列：攒批、去重排序、每日额度、静默时段、红线拒绝。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lingbrain.config import Settings
from lingbrain.db import Database
from lingbrain.share import (
    MAX_BATCH_SIZE,
    Discovery,
    SharePolicyError,
    ShareService,
    assert_no_comment_reply,
    assert_no_mention,
    assert_no_secrets,
    assert_shareable,
    build_batch,
    build_share_router,
    dedup_key,
    normalize_url,
    order_discoveries,
)
from lingbrain.share_routes import register_share_routes

TOKEN = "test-token"
LOCAL = timezone(timedelta(hours=8))


def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path, auth_token=TOKEN, heartbeat_seconds=3600)


def headers() -> dict[str, str]:
    return {"X-Ling-Token": TOKEN}


def make_db(tmp_path) -> Database:
    return Database(tmp_path / "share.db")


def noon(day: str = "2026-10-31") -> datetime:
    """本地中午 12:00 —— 一定不在默认静默时段（23:30-07:30）里。"""
    return datetime.fromisoformat(f"{day}T12:00:00").replace(tzinfo=LOCAL)


class OpenGate:
    """状态机替身：随时可以主动开口。"""

    def should_speak(self, now=None) -> bool:
        return True

    def proactive_hint(self, now=None) -> dict:
        return {"allowed": True, "reasons": [], "state": {"mood": "平静", "energy": 80}}


class QuietGate:
    """状态机替身：静默时段 / 睡着了。"""

    def __init__(self, reason: str = "静默时段不主动"):
        self.reason = reason

    def should_speak(self, now=None) -> bool:
        return False

    def proactive_hint(self, now=None) -> dict:
        return {"allowed": False, "reasons": [self.reason], "state": {"sleeping": True}}


class BoomGate:
    """状态机自身抛异常：必须按「不主动」处理，不能崩。"""

    def should_speak(self, now=None) -> bool:
        raise RuntimeError("状态机炸了")

    def proactive_hint(self, now=None) -> dict:
        raise RuntimeError("状态机炸了")


def disc(url: str, comment: str = "这条挺有意思", title: str = "", score: float = 0.0, ts: str | None = None):
    kwargs = {} if ts is None else {"discovered_at": ts}
    return Discovery(url=url, comment=comment, title=title, score=score, **kwargs)


# --- 红线：绝不@人 / 不回复评论 / 不发公共评论 / 不碰密码验证码 ----------------


def test_mention_is_rejected():
    for text in ("@小明 看看这个", "＠阿星 好文", "分享给@"):
        with pytest.raises(SharePolicyError) as excinfo:
            assert_no_mention(text)
        assert excinfo.value.code == "mention_forbidden"


def test_comment_reply_is_rejected():
    for text in ("回复楼主的看法", "在评论区见", "转发评论里说得好", "回他一句"):
        with pytest.raises(SharePolicyError) as excinfo:
            assert_no_comment_reply(text)
        assert excinfo.value.code == "comment_reply_forbidden"


def test_secrets_are_rejected():
    for text in ("我的密码是123456", "验证码 8888", "password=abc", "token=xyz"):
        with pytest.raises(SharePolicyError) as excinfo:
            assert_no_secrets(text)
        assert excinfo.value.code == "secret_forbidden"


def test_assert_shareable_allows_clean_item():
    assert_shareable("https://www.bilibili.com/video/BV1xx411c7mD", "这片子安静得刚好")


def test_assert_shareable_requires_comment():
    with pytest.raises(SharePolicyError) as excinfo:
        assert_shareable("https://example.com/a", "   ")
    assert excinfo.value.code == "empty_comment"


def test_assert_shareable_rejects_long_comment():
    with pytest.raises(SharePolicyError) as excinfo:
        assert_shareable("https://example.com/a", "好" * 200)
    assert excinfo.value.code == "comment_too_long"


def test_batch_skips_offending_item_instead_of_publishing_it():
    items = build_batch(
        [
            disc("https://www.bilibili.com/video/BV1aa411c7mD/", "@小明 快看"),
            disc("https://www.bilibili.com/video/BV1bb411c7mD/", "这片子安静得刚好"),
        ]
    )
    assert len(items) == 1
    assert "BV1bb411c7mD" in items[0].url
    assert "@" not in items[0].comment


@pytest.mark.parametrize("forbidden", ["@某人", "回复楼主", "我的密码是1", "验证码 1234"])
def test_build_batch_rejects_each_red_line(forbidden):
    rejected: list[dict] = []
    items = build_batch(
        [disc("https://www.bilibili.com/video/BV1cc411c7mD/", forbidden)],
        rejected=rejected,
    )
    assert items == []
    assert len(rejected) == 1


# --- 排序与去重 ---------------------------------------------------------


def test_normalize_url_strips_tracking():
    a = normalize_url("https://WWW.Bilibili.com/video/BV1xx411c7mD/?spm_id_from=333&p=2")
    b = normalize_url("https://www.bilibili.com/video/BV1xx411c7mD?p=2")
    assert a == b
    assert "spm_id_from" not in a


def test_dedup_key_uses_video_id():
    k1 = dedup_key("https://www.bilibili.com/video/BV1xx411c7mD/?spm_id_from=1")
    k2 = dedup_key("https://www.bilibili.com/video/bv1xx411c7md/")
    assert k1 == k2 == "BV:BV1XX411C7MD"


def test_order_prefers_score_then_recency():
    old = disc("https://example.com/old", score=5, ts="2026-10-01T00:00:00+00:00")
    new = disc("https://example.com/new", score=5, ts="2026-10-02T00:00:00+00:00")
    top = disc("https://example.com/top", score=9, ts="2026-09-01T00:00:00+00:00")
    ordered = order_discoveries([old, new, top])
    assert [d.url for d in ordered][0] == "https://example.com/top"
    assert [d.url for d in ordered][1:] == ["https://example.com/new", "https://example.com/old"]


def test_order_dedups_same_video():
    ordered = order_discoveries(
        [
            disc("https://www.bilibili.com/video/BV1xx411c7mD/?spm_id_from=a"),
            disc("https://www.bilibili.com/video/bv1xx411c7md/"),
        ]
    )
    assert len(ordered) == 1


def test_batch_size_is_capped_at_three():
    many = [
        disc(f"https://example.com/{i}", comment=f"第{i}条") for i in range(10)
    ]
    assert len(build_batch(many)) == MAX_BATCH_SIZE == 3


def test_batch_can_be_single_item():
    items = build_batch([disc("https://example.com/only", comment="只有一条")])
    assert len(items) == 1


def test_empty_queue_yields_empty_batch():
    assert build_batch([]) == []


# --- 每日额度 -----------------------------------------------------------


def test_daily_limit_is_three_batches(tmp_path):
    db = make_db(tmp_path)
    try:
        svc = ShareService(db, OpenGate())
        for i in range(3):
            result = svc.publish(
                [disc(f"https://www.bilibili.com/video/BV1aa411c7m{i}/", comment=f"第{i}批")],
                now=noon(),
            )
            assert result["published"] is True, result["reasons"]
        blocked = svc.publish(
            [disc("https://www.bilibili.com/video/BV1zz411c7mD/", comment="第四批")],
            now=noon(),
        )
        assert blocked["published"] is False
        assert blocked["reason"] == "blocked"
        assert any("上限 3 批" in r for r in blocked["reasons"])
        assert svc.published_today(noon()) == 3
        assert svc.status(noon())["remaining"] == 0
    finally:
        db.close()


def test_limit_resets_next_day(tmp_path):
    db = make_db(tmp_path)
    try:
        svc = ShareService(db, OpenGate())
        for i in range(3):
            svc.publish([disc(f"https://example.com/d1-{i}", comment="今天")], now=noon("2026-10-31"))
        next_day = svc.publish(
            [disc("https://example.com/d2", comment="明天")], now=noon("2026-11-01")
        )
        assert next_day["published"] is True
        assert next_day["batch"]["day"] == "2026-11-01"
    finally:
        db.close()


def test_blocked_publish_is_recorded_not_silent(tmp_path):
    db = make_db(tmp_path)
    try:
        svc = ShareService(db, QuietGate("静默时段不主动"))
        result = svc.publish([disc("https://example.com/a", comment="好")], now=noon())
        assert result["published"] is False
        assert "静默时段不主动" in result["reasons"]
        kinds = [e["kind"] for e in db.list_events(20)]
        assert "share.batch_blocked" in kinds
        history = svc.history()
        assert history and history[0]["payload"]["reasons"]
    finally:
        db.close()


# --- 静默时段 / 睡眠（复用 state_machine 的判定）--------------------------


def test_quiet_gate_blocks_publishing(tmp_path):
    db = make_db(tmp_path)
    try:
        svc = ShareService(db, QuietGate("睡着了不主动"))
        allowed, reasons = svc.can_publish()
        assert allowed is False
        assert "睡着了不主动" in reasons
    finally:
        db.close()


def test_broken_gate_fails_closed(tmp_path):
    db = make_db(tmp_path)
    try:
        svc = ShareService(db, BoomGate())
        allowed, reasons = svc.can_publish()
        assert allowed is False
        assert reasons
        # 状态机异常也必须能拿到 status，不炸
        assert svc.status()["allowed"] is False
    finally:
        db.close()


def test_missing_gate_defaults_to_not_speaking(tmp_path):
    db = make_db(tmp_path)
    try:
        svc = ShareService(db)
        allowed, reasons = svc.can_publish()
        assert allowed is False
        assert "状态机不可用" in reasons[0]
    finally:
        db.close()


def test_force_bypasses_gate_but_never_red_lines(tmp_path):
    db = make_db(tmp_path)
    try:
        svc = ShareService(db, QuietGate())
        forced = svc.publish(
            [disc("https://www.bilibili.com/video/BV1rd411c7mD/", comment="主人想看我有什么")],
            now=noon(),
            force=True,
        )
        assert forced["published"] is True
        # 但带着 @ 的东西，force 也不能发
        bad = svc.publish(
            [disc("https://www.bilibili.com/video/BV1zz411c7mD/", comment="@某人 快看")],
            now=noon(),
            force=True,
        )
        assert bad["published"] is False
    finally:
        db.close()


def test_real_state_machine_quiet_hours_block(tmp_path):
    """接真的 StateMachine：凌晨在默认静默时段里，必须不发。"""
    from lingbrain.state_machine import StateMachine

    db = make_db(tmp_path)
    try:
        machine = StateMachine(db, quiet_hours="23:30-07:30", utc_offset_minutes=480)
        svc = ShareService(db, machine)
        night = datetime.fromisoformat("2026-10-31T02:00:00").replace(tzinfo=LOCAL)
        allowed, reasons = svc.can_publish(night)
        assert allowed is False
        assert any("静默" in r for r in reasons)
        daytime = datetime.fromisoformat("2026-10-31T14:00:00").replace(tzinfo=LOCAL)
        assert svc.can_publish(daytime)[0] is True
    finally:
        db.close()


# --- 文案形态：只有链接 + 一句评价 ---------------------------------------


def test_text_has_link_and_single_comment(tmp_path):
    db = make_db(tmp_path)
    try:
        svc = ShareService(db, OpenGate())
        result = svc.publish(
            [
                disc("https://www.bilibili.com/video/BV1aa411c7mD/", "这片子的色调像傍晚的海", title="海边日落"),
                disc("https://www.bilibili.com/video/BV1bb411c7mD/", "她说得很轻，但很重"),
            ],
            now=noon(),
        )
        assert result["published"] is True
        text = result["batch"]["text"]
        assert "https://www.bilibili.com/video/BV1aa411c7mD" in text
        assert "这片子的色调像傍晚的海" in text
        assert "她说得很轻，但很重" in text
        assert text.count("我的评价：") == 2
        # 不是公共评论：没有任何 @ 与评论动作字样
        assert "@" not in text
        for word in ("回复", "评论他人", "下面评论"):
            assert word not in text
        assert result["batch"]["size"] == 2
    finally:
        db.close()


def test_cross_batch_dedup_skips_already_shared(tmp_path):
    db = make_db(tmp_path)
    try:
        svc = ShareService(db, OpenGate())
        link = "https://www.bilibili.com/video/BV1aa411c7mD/"
        first = svc.publish([disc(link, "第一次")], now=noon())
        assert first["published"] is True
        second = svc.publish([disc(link + "?spm_id_from=x", "换个链接同一视频")], now=noon())
        assert second["published"] is False
        assert second["reason"] == "empty"
    finally:
        db.close()


# --- HTTP 契约 ----------------------------------------------------------


def make_client(tmp_path, gate=None):
    db = make_db(tmp_path)
    app = FastAPI()
    app.include_router(build_share_router(settings(tmp_path), db, gate or OpenGate()))
    return db, TestClient(app)


def test_status_requires_auth(tmp_path):
    db, client = make_client(tmp_path)
    try:
        assert client.get("/v1/share/status").status_code == 401
        body = client.get("/v1/share/status", headers=headers()).json()
        assert body["daily_batch_limit"] == 3
        assert body["policies"]["mention_allowed"] is False
        assert body["policies"]["public_comment_allowed"] is False
        assert body["policies"]["reply_to_comment_allowed"] is False
    finally:
        client.close()
        db.close()


def test_publish_endpoint_batches(tmp_path):
    db, client = make_client(tmp_path)
    try:
        payload = {
            "discoveries": [
                {"url": "https://www.bilibili.com/video/BV1aa411c7mD/", "comment": "安静得好听"},
                {"url": "https://www.bilibili.com/video/BV1bb411c7mD/", "comment": "像在讲我的事"},
            ]
        }
        response = client.post("/v1/share/publish", headers=headers(), json=payload)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["published"] is True
        assert body["batch"]["size"] == 2
        assert "@" not in body["batch"]["text"]
    finally:
        client.close()
        db.close()


def test_publish_endpoint_rejects_mention_at_entry(tmp_path):
    db, client = make_client(tmp_path)
    try:
        response = client.post(
            "/v1/share/publish",
            headers=headers(),
            json={"discoveries": [{"url": "https://example.com/a", "comment": "@小明 快看"}]},
        )
        assert response.status_code == 422
    finally:
        client.close()
        db.close()


def test_guard_endpoint_flags_red_lines(tmp_path):
    db, client = make_client(tmp_path)
    try:
        ok = client.post(
            "/v1/share/guard",
            headers=headers(),
            json={"discoveries": [{"url": "https://example.com/a", "comment": "挺好的"}]},
        )
        assert ok.status_code == 200 and ok.json()["ok"] is True
        bad = client.post(
            "/v1/share/guard",
            headers=headers(),
            json={"discoveries": [{"url": "https://example.com/a", "comment": "我的密码是 1234"}]},
        )
        assert bad.status_code == 422
        assert bad.json()["detail"]["error"] == "share_policy_violation"
    finally:
        client.close()
        db.close()


def test_preview_does_not_write_state(tmp_path):
    db, client = make_client(tmp_path)
    try:
        before = len(db.list_events(100))
        response = client.post(
            "/v1/share/preview",
            headers=headers(),
            json={"discoveries": [{"url": "https://example.com/a", "comment": "看看"}]},
        )
        assert response.status_code == 200
        assert response.json()["items"]
        assert len(db.list_events(100)) == before
    finally:
        client.close()
        db.close()


def test_quiet_gate_over_http_blocks_publish(tmp_path):
    db, client = make_client(tmp_path, QuietGate("静默时段不主动"))
    try:
        response = client.post(
            "/v1/share/publish",
            headers=headers(),
            json={"discoveries": [{"url": "https://example.com/a", "comment": "好"}]},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["published"] is False
        assert "静默时段不主动" in body["reasons"]
    finally:
        client.close()
        db.close()


def test_register_share_routes_mounts_endpoints(tmp_path):
    db = make_db(tmp_path)
    app = FastAPI()
    try:
        service = register_share_routes(app, settings(tmp_path), db, OpenGate())
        assert app.state.share_service is service
        with TestClient(app) as client:
            assert client.get("/v1/share/status").status_code == 401
            assert client.get("/v1/share/status", headers=headers()).status_code == 200
    finally:
        db.close()
