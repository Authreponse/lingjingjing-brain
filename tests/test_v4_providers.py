"""V4 提供商注册表 + 并发路由 + runtime.json 测试（task-19）。

按 docs/api-v4.md 第 2、4 节验证契约，重点是：
* api_key **只写不读**（任何响应都不能带出密钥）
* 数量上限 20 / 名称唯一 / 内置 echo 不可删
* base_url 拒绝云元数据地址
* single / concurrent 路由校验与能力分配
* single 失败 → 503；concurrent 失败 → 故障转移并记录
* runtime.json 启动写、退出删
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from lingbrain.db import Database, SCHEMA_VERSION
from lingbrain.model_providers import ModelUnavailable
from lingbrain.provider_routes import create_provider_router
from lingbrain.providers import (
    BUILTIN_ECHO_ID,
    CAPABILITIES,
    MAX_ENABLED,
    MAX_PROVIDERS,
    Provider,
    ProviderConflict,
    ProviderForbidden,
    ProviderLimitExceeded,
    ProviderNotFound,
    ProviderRegistry,
    ProviderRouter,
    ProviderValidationError,
    assign_capabilities,
    remove_runtime,
    runtime_path,
    validate_base_url,
    write_runtime,
)

TOKEN = "test-token"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def make_db(tmp_path) -> Database:
    return Database(tmp_path / "providers.db")


def make_registry(tmp_path) -> ProviderRegistry:
    return ProviderRegistry(make_db(tmp_path))


def add_provider(registry, name="P1", *, kind="openai_compatible",
                 base_url="http://127.0.0.1:9999/v1", model="m", caps=None):
    provider, _ = registry.create(
        name=name, kind=kind, base_url=base_url, model=model,
        capabilities=caps or ["chat"],
    )
    return provider


def build_app(tmp_path) -> tuple[FastAPI, ProviderRegistry]:
    registry = make_registry(tmp_path)
    app = FastAPI()

    async def authorized(x_ling_token: str = Header(default="")) -> None:
        if x_ling_token != TOKEN:
            raise HTTPException(status_code=401, detail="认证失败")

    app.include_router(create_provider_router(registry, authorize=authorized))
    return app, registry


def headers() -> dict[str, str]:
    return {"X-Ling-Token": TOKEN}


# ---------------------------------------------------------------------------
# 1. schema 迁移
# ---------------------------------------------------------------------------


def test_schema_version_is_two(tmp_path):
    db = make_db(tmp_path)
    assert SCHEMA_VERSION == 2
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_old_database_is_upgraded_in_place(tmp_path):
    """模拟 V3 老库：删掉新表并把版本退回 1，重新打开应自动补齐。"""
    path = tmp_path / "old.db"
    db = Database(path)
    with db.conn:
        db.conn.execute("DROP TABLE provider")
        db.conn.execute("DROP TABLE routing")
        db.conn.execute("DELETE FROM schema_migrations WHERE version=2")
        db.conn.execute("PRAGMA user_version=1")
    db.close()

    upgraded = Database(path)
    assert upgraded.conn.execute("PRAGMA user_version").fetchone()[0] == 2
    tables = {
        row[0]
        for row in upgraded.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"provider", "routing"} <= tables
    versions = [
        row[0]
        for row in upgraded.conn.execute("SELECT version FROM schema_migrations ORDER BY version")
    ]
    assert versions == [1, 2]


# ---------------------------------------------------------------------------
# 2. 内置 echo + api_key 只写不读
# ---------------------------------------------------------------------------


def test_builtin_echo_always_exists(tmp_path):
    registry = make_registry(tmp_path)
    builtin = registry.get(BUILTIN_ECHO_ID)
    assert builtin is not None
    assert builtin.is_builtin is True
    assert builtin.kind == "echo"


def test_builtin_echo_cannot_be_deleted(tmp_path):
    registry = make_registry(tmp_path)
    with pytest.raises(ProviderForbidden):
        registry.delete(BUILTIN_ECHO_ID)


def test_builtin_delete_returns_403_over_http(tmp_path):
    app, _ = build_app(tmp_path)
    with TestClient(app) as client:
        response = client.delete(f"/v1/providers/{BUILTIN_ECHO_ID}", headers=headers())
        assert response.status_code == 403


def test_api_key_is_write_only(tmp_path):
    """最关键的一条：密钥写进去后，任何读路径都不能把它吐出来。"""
    registry = make_registry(tmp_path)
    secret = "sk-super-secret-value-12345"
    provider, _ = registry.create(
        name="有密钥的", kind="openai_compatible",
        base_url="http://127.0.0.1:9999/v1", model="m", api_key=secret,
    )
    assert provider.has_api_key is True
    payload = json.dumps(provider.out(), ensure_ascii=False)
    assert secret not in payload
    assert "api_key" not in provider.out()

    # 整份列表序列化里也不能出现
    everything = json.dumps([p.out() for p in registry.list()], ensure_ascii=False)
    assert secret not in everything


def test_api_key_never_leaks_through_http(tmp_path):
    app, registry = build_app(tmp_path)
    secret = "sk-http-secret-abc"
    with TestClient(app) as client:
        created = client.post(
            "/v1/providers", headers=headers(),
            json={"name": "HTTP 提供商", "kind": "openai_compatible",
                  "base_url": "http://127.0.0.1:9999/v1", "model": "m", "api_key": secret},
        )
        assert created.status_code == 201, created.text
        assert secret not in created.text
        listed = client.get("/v1/providers", headers=headers())
        assert secret not in listed.text
        item = next(i for i in listed.json()["items"] if i["name"] == "HTTP 提供商")
        assert item["has_api_key"] is True


def test_api_key_update_semantics(tmp_path):
    """契约：不传/空字符串=不修改；null=清除；非空=覆盖。"""
    registry = make_registry(tmp_path)
    provider, _ = registry.create(
        name="A", kind="openai_compatible",
        base_url="http://127.0.0.1:9999/v1", model="m", api_key="original",
    )

    # 空字符串 = 不修改
    updated, _ = registry.update(provider.id, api_key="")
    assert updated.api_key == "original"

    # 不传 = 不修改
    updated, _ = registry.update(provider.id, model="m2")
    assert updated.api_key == "original"

    # 非空 = 覆盖
    updated, _ = registry.update(provider.id, api_key="replaced")
    assert updated.api_key == "replaced"

    # null = 清除
    updated, _ = registry.update(provider.id, api_key=None)
    assert updated.api_key == ""
    assert updated.has_api_key is False


def test_api_key_update_semantics_over_http(tmp_path):
    app, registry = build_app(tmp_path)
    with TestClient(app) as client:
        created = client.post(
            "/v1/providers", headers=headers(),
            json={"name": "B", "kind": "openai_compatible",
                  "base_url": "http://127.0.0.1:9999/v1", "model": "m", "api_key": "k1"},
        ).json()["created"]
        pid = created["id"]

        # 空字符串不修改
        client.put(f"/v1/providers/{pid}", headers=headers(), json={"api_key": ""})
        assert registry.require(pid).api_key == "k1"

        # null 清除
        client.put(f"/v1/providers/{pid}", headers=headers(), json={"api_key": None})
        assert registry.require(pid).api_key == ""


# ---------------------------------------------------------------------------
# 3. 校验规则
# ---------------------------------------------------------------------------


def test_provider_limit_is_twenty(tmp_path):
    registry = make_registry(tmp_path)
    # 已有内置 1 个，还能加 19 个
    for i in range(MAX_PROVIDERS - 1):
        add_provider(registry, name=f"P{i}", base_url=f"http://127.0.0.1:{9000 + i}/v1")
    assert registry.count() == MAX_PROVIDERS
    with pytest.raises(ProviderLimitExceeded):
        add_provider(registry, name="overflow")


def test_name_must_be_unique(tmp_path):
    registry = make_registry(tmp_path)
    add_provider(registry, name="重复名")
    with pytest.raises(ProviderConflict):
        add_provider(registry, name="重复名", base_url="http://127.0.0.1:9998/v1")


def test_metadata_address_is_rejected(tmp_path):
    registry = make_registry(tmp_path)
    for url in ("http://169.254.169.254/latest/meta-data", "http://100.100.100.200/v1"):
        with pytest.raises(ProviderValidationError):
            registry.create(name=f"m{url[-4:]}", kind="openai_compatible",
                            base_url=url, model="m")


def test_base_url_must_be_http(tmp_path):
    registry = make_registry(tmp_path)
    for bad in ("", "ftp://x/v1", "file:///etc/passwd", "javascript:alert(1)"):
        with pytest.raises(ProviderValidationError):
            validate_base_url(bad)


def test_private_lan_url_only_warns():
    # 内网非回环只警告，不阻断
    warnings = validate_base_url("http://192.168.1.10:11434/v1")
    assert warnings


def test_loopback_has_no_warning():
    assert validate_base_url("http://127.0.0.1:11434/v1") == []


def test_invalid_capability_rejected(tmp_path):
    registry = make_registry(tmp_path)
    with pytest.raises(ProviderValidationError):
        registry.create(name="bad caps", kind="ollama",
                        base_url="http://127.0.0.1:1/v1", model="m",
                        capabilities=["telepathy"])


def test_create_route_validates_and_returns_201(tmp_path):
    app, _ = build_app(tmp_path)
    with TestClient(app) as client:
        ok = client.post("/v1/providers", headers=headers(),
                         json={"name": "新", "kind": "ollama",
                               "base_url": "http://127.0.0.1:11434/v1", "model": "qwen"})
        assert ok.status_code == 201
        assert ok.json()["created"]["status"] == "unchecked"

        bad = client.post("/v1/providers", headers=headers(),
                          json={"name": "", "kind": "ollama",
                                "base_url": "http://127.0.0.1:1/v1", "model": "m"})
        assert bad.status_code == 422

        meta = client.post("/v1/providers", headers=headers(),
                           json={"name": "meta", "kind": "ollama",
                                 "base_url": "http://169.254.169.254/v1", "model": "m"})
        assert meta.status_code == 422
        assert "169.254" in meta.text or "元数据" in meta.text


# ---------------------------------------------------------------------------
# 4. 路由配置
# ---------------------------------------------------------------------------


def test_single_requires_active_provider(tmp_path):
    registry = make_registry(tmp_path)
    with pytest.raises(ProviderValidationError):
        registry.set_routing(mode="single")


def test_single_requires_existing_provider(tmp_path):
    registry = make_registry(tmp_path)
    with pytest.raises(ProviderValidationError):
        registry.set_routing(mode="single", active_provider_id="nope")


def test_single_assigns_all_tasks_to_one(tmp_path):
    registry = make_registry(tmp_path)
    provider = add_provider(registry)
    routing = registry.set_routing(mode="single", active_provider_id=provider.id)
    assert routing.mode == "single"
    assert set(routing.assignments.values()) == {provider.id}
    # 注册表只管 LLM 能力；生图/语音不在此列（lead 已拍板收敛）
    assert set(routing.assignments) == {"chat", "extract"}


def test_concurrent_requires_1_to_4(tmp_path):
    registry = make_registry(tmp_path)
    providers = [add_provider(registry, name=f"C{i}", base_url=f"http://127.0.0.1:{8000+i}/v1")
                 for i in range(MAX_ENABLED + 1)]
    with pytest.raises(ProviderValidationError):
        registry.set_routing(mode="concurrent", enabled_ids=[])
    with pytest.raises(ProviderValidationError):
        registry.set_routing(mode="concurrent",
                             enabled_ids=[p.id for p in providers[: MAX_ENABLED + 1]])


def test_concurrent_rejects_duplicates(tmp_path):
    registry = make_registry(tmp_path)
    provider = add_provider(registry)
    with pytest.raises(ProviderValidationError):
        registry.set_routing(mode="concurrent", enabled_ids=[provider.id, provider.id])


def test_concurrent_assigns_by_capability(tmp_path):
    registry = make_registry(tmp_path)
    full = add_provider(registry, name="全能", base_url="http://127.0.0.1:9001/v1",
                        caps=["chat", "extract"])
    chat_only = add_provider(registry, name="只聊天", base_url="http://127.0.0.1:9002/v1",
                             caps=["chat"])
    routing = registry.set_routing(mode="concurrent", enabled_ids=[full.id, chat_only.id])
    assert routing.assignments["chat"] == full.id
    # extract 只能给有能力的那台
    assert routing.assignments["extract"] == full.id
    # image/tts 不属于本注册表，永远不出现
    assert "image" not in routing.assignments
    assert "tts" not in routing.assignments


def test_image_and_tts_capabilities_are_rejected(tmp_path):
    """生图/语音走各自后端；注册表若还接受这两个能力，会误导配置界面。"""
    registry = make_registry(tmp_path)
    for cap in ("image", "tts"):
        with pytest.raises(ProviderValidationError) as excinfo:
            registry.create(name=f"不可用-{cap}", kind="openai_compatible",
                            base_url="http://127.0.0.1:9401/v1", model="m",
                            capabilities=[cap])
        assert cap in str(excinfo.value)


def test_capabilities_list_is_chat_and_extract_only():
    assert CAPABILITIES == ("chat", "extract")


def test_image_tts_capability_rejected_over_http(tmp_path):
    app, _ = build_app(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/v1/providers", headers=headers(),
            json={"name": "想接管生图", "kind": "ollama",
                  "base_url": "http://127.0.0.1:9402/v1", "model": "m",
                  "capabilities": ["image"]},
        )
        assert response.status_code == 422
        assert "image" in response.text


def test_assign_prefers_lowest_latency_then_ok_status(tmp_path):
    registry = make_registry(tmp_path)
    slow_ok = add_provider(registry, name="慢", base_url="http://127.0.0.1:9101/v1")
    fast_ok = add_provider(registry, name="快", base_url="http://127.0.0.1:9102/v1")
    unseen = add_provider(registry, name="没测过", base_url="http://127.0.0.1:9103/v1")
    registry.mark_status(slow_ok.id, "ok")
    registry.mark_status(fast_ok.id, "ok")
    # 手动给延迟：快的最快
    with registry.db._lock, registry.db.conn:
        registry.db.conn.execute("UPDATE provider SET latency_ms=500 WHERE id=?", (slow_ok.id,))
        registry.db.conn.execute("UPDATE provider SET latency_ms=50 WHERE id=?", (fast_ok.id,))
    assignment = assign_capabilities(registry.list())
    assert assignment["chat"] == fast_ok.id  # 未探活的排后面


def test_routing_route_rejects_bad_mode(tmp_path):
    app, _ = build_app(tmp_path)
    with TestClient(app) as client:
        bad = client.post("/v1/providers/routing", headers=headers(),
                          json={"mode": "quantum"})
        assert bad.status_code == 422


def test_routing_route_is_not_shadowed_by_provider_id(tmp_path):
    """'/v1/providers/routing' 不能被当成 provider_id='routing'。"""
    app, registry = build_app(tmp_path)
    provider = add_provider(registry)
    with TestClient(app) as client:
        response = client.post("/v1/providers/routing", headers=headers(),
                               json={"mode": "single", "active_provider_id": provider.id})
        assert response.status_code == 200, response.text
        assert response.json()["routing"]["mode"] == "single"


def test_deleting_provider_cleans_routing(tmp_path):
    registry = make_registry(tmp_path)
    provider = add_provider(registry)
    registry.set_routing(mode="single", active_provider_id=provider.id)
    registry.delete(provider.id)
    routing = registry.routing()
    assert routing.active_provider_id is None
    assert provider.id not in routing.enabled_ids


def test_unknown_provider_returns_404(tmp_path):
    app, _ = build_app(tmp_path)
    with TestClient(app) as client:
        assert client.delete("/v1/providers/nope", headers=headers()).status_code == 404
        assert client.post("/v1/providers/nope/test", headers=headers()).status_code == 404


# ---------------------------------------------------------------------------
# 5. 故障转移
# ---------------------------------------------------------------------------


class _FakeProvider:
    """假提供商实例：可控成功/失败，用来验证路由的 failover 行为。"""

    def __init__(self, name, *, fail=False, reply="好的"):
        self.name = name
        self.fail = fail
        self.reply = reply
        self.calls = 0

    async def chat(self, messages, system, **kwargs):
        self.calls += 1
        if self.fail:
            raise ModelUnavailable(f"{self.name} 挂了")
        return self.reply


def _patch_build(monkeypatch, mapping):
    import lingbrain.providers as mod

    def fake(provider, *, timeout_seconds=60.0):
        return mapping[provider.id]

    monkeypatch.setattr(mod, "build_provider_instance", fake)


def test_single_mode_failure_raises(tmp_path, monkeypatch):
    registry = make_registry(tmp_path)
    provider = add_provider(registry)
    registry.set_routing(mode="single", active_provider_id=provider.id)
    broken = _FakeProvider("broken", fail=True)
    _patch_build(monkeypatch, {provider.id: broken})

    router = ProviderRouter(registry)
    with pytest.raises(ModelUnavailable):
        asyncio.run(router.chat_with_failover([], "sys"))
    assert broken.calls == 1


def test_single_mode_any_error_becomes_unavailable(tmp_path, monkeypatch):
    """single 模式抛什么异常都要归一成 ModelUnavailable，路由层才能稳定映射 503。"""

    class Exploding:
        name = "boom"

        async def chat(self, messages, system, **kwargs):
            raise RuntimeError("底层网络断了")

    registry = make_registry(tmp_path)
    provider = add_provider(registry)
    registry.set_routing(mode="single", active_provider_id=provider.id)
    monkeypatch.setattr(
        "lingbrain.providers.build_provider_instance", lambda p, timeout_seconds=60.0: Exploding()
    )
    router = ProviderRouter(registry)
    with pytest.raises(ModelUnavailable):
        asyncio.run(router.chat_with_failover([], "sys"))
    # 失败状态要落库，不隐藏
    assert registry.require(provider.id).status == "down"


def test_concurrent_mode_fails_over(tmp_path, monkeypatch):
    registry = make_registry(tmp_path)
    first = add_provider(registry, name="一", base_url="http://127.0.0.1:9201/v1")
    second = add_provider(registry, name="二", base_url="http://127.0.0.1:9202/v1")
    registry.set_routing(mode="concurrent", enabled_ids=[first.id, second.id])
    broken = _FakeProvider("一", fail=True)
    healthy = _FakeProvider("二", reply="我在")
    _patch_build(monkeypatch, {first.id: broken, second.id: healthy})

    router = ProviderRouter(registry)
    reply, used, failovers = asyncio.run(router.chat_with_failover([], "sys"))
    assert reply == "我在"
    assert used.id == second.id
    assert len(failovers) == 1
    assert failovers[0]["provider_id"] == first.id
    # 失败要写进状态，不隐藏
    assert registry.require(first.id).status == "down"


def test_concurrent_all_fail_raises_with_details(tmp_path, monkeypatch):
    registry = make_registry(tmp_path)
    a = add_provider(registry, name="A", base_url="http://127.0.0.1:9301/v1")
    b = add_provider(registry, name="B", base_url="http://127.0.0.1:9302/v1")
    registry.set_routing(mode="concurrent", enabled_ids=[a.id, b.id])
    _patch_build(monkeypatch, {a.id: _FakeProvider("A", fail=True),
                               b.id: _FakeProvider("B", fail=True)})
    router = ProviderRouter(registry)
    with pytest.raises(ModelUnavailable) as excinfo:
        asyncio.run(router.chat_with_failover([], "sys"))
    assert "A" in str(excinfo.value) and "B" in str(excinfo.value)


def test_no_routing_falls_back_to_default_model(tmp_path):
    registry = make_registry(tmp_path)
    default = _FakeProvider("default", reply="默认回答")
    router = ProviderRouter(registry, default=default)
    reply, used, failovers = asyncio.run(router.chat_with_failover([], "sys"))
    assert reply == "默认回答"
    assert failovers == []


def test_no_routing_and_no_default_raises(tmp_path):
    registry = make_registry(tmp_path)
    router = ProviderRouter(registry, default=None)
    with pytest.raises(ModelUnavailable):
        asyncio.run(router.chat_with_failover([], "sys"))


# ---------------------------------------------------------------------------
# 6. runtime.json（契约第 4 节）
# ---------------------------------------------------------------------------


def test_runtime_json_written_and_removed(tmp_path):
    path = write_runtime(tmp_path, port=7100, token="tok", pid=4321)
    assert path == runtime_path(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["port"] == 7100
    assert payload["token"] == "tok"
    assert payload["pid"] == 4321
    assert payload["started_at"]

    remove_runtime(tmp_path)
    assert not path.exists()
    # 重复删除不报错
    remove_runtime(tmp_path)


def test_runtime_json_lifecycle_in_app(tmp_path):
    """真正起一个 app：启动写 runtime.json，退出删掉。"""
    from lingbrain.app import create_app
    from lingbrain.config import Settings

    settings = Settings(data_dir=tmp_path, auth_token="t", model_provider="echo",
                        heartbeat_seconds=3600)
    app = create_app(settings)
    path = runtime_path(tmp_path)
    assert not path.exists()  # 还没启动
    with TestClient(app) as client:
        assert path.exists()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["token"] == "t"
        assert payload["port"] == settings.port
        assert client.get("/v1/health", headers={"X-Ling-Token": "t"}).status_code == 200
    assert not path.exists()


# ---------------------------------------------------------------------------
# 7. 认证
# ---------------------------------------------------------------------------


def test_provider_routes_require_auth(tmp_path):
    app, _ = build_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/v1/providers").status_code == 401
        assert client.post("/v1/providers", json={}).status_code == 401
        assert client.post("/v1/providers/routing", json={"mode": "single"}).status_code == 401
