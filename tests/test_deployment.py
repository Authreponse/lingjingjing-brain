"""部署模式持久化端点测试（task-22）。

覆盖：默认值、读写往返、mode 枚举校验、server 模式必须有合法 http(s) URL、
token 允许为空、原子写不留临时文件、损坏文件降级、认证。

背景：前端写不了 data/，launcher 只读文件，所以这个端点是两者与磁盘之间的
**唯一写入方**，写入的原子性与校验在这里必须可靠。
"""

from __future__ import annotations

import json
import threading

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from lingbrain.deployment import (
    DEFAULT_MODE,
    DEPLOYMENT_FILENAME,
    Deployment,
    DeploymentError,
    MODES,
    create_deployment_router,
    deployment_path,
    normalize,
    read_deployment,
    validate_server_url,
    write_deployment,
)

TOKEN = "test-token"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def build_app(tmp_path) -> FastAPI:
    app = FastAPI()

    async def authorized(x_ling_token: str = Header(default="")) -> None:
        if x_ling_token != TOKEN:
            raise HTTPException(status_code=401, detail="认证失败")

    app.include_router(create_deployment_router(tmp_path, authorize=authorized))
    return app


def headers() -> dict[str, str]:
    return {"X-Ling-Token": TOKEN}


# ---------------------------------------------------------------------------
# 1. 读取：缺失时返回默认值 + defaulted:true
# ---------------------------------------------------------------------------


def test_missing_file_returns_default(tmp_path):
    deployment = read_deployment(tmp_path)
    assert deployment.mode == "local"
    assert deployment.server_url == ""
    assert deployment.token == ""
    assert deployment.defaulted is True
    assert deployment.reason


def test_get_missing_file_over_http(tmp_path):
    with TestClient(build_app(tmp_path)) as client:
        body = client.get("/v1/deployment", headers=headers()).json()
        assert body["mode"] == "local"
        assert body["server_url"] == ""
        assert body["token"] == ""
        assert body["defaulted"] is True


def test_default_mode_constant():
    assert DEFAULT_MODE == "local"
    assert set(MODES) == {"local", "terminal", "server"}


# ---------------------------------------------------------------------------
# 2. 写入 + 读回
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["local", "terminal", "server"])
def test_put_then_get_roundtrip(tmp_path, mode):
    app = build_app(tmp_path)
    payload = {
        "mode": mode,
        "server_url": "http://192.168.1.5:7100" if mode == "server" else "",
        "token": "" if mode != "server" else "abc",
    }
    with TestClient(app) as client:
        put = client.put("/v1/deployment", headers=headers(), json=payload)
        assert put.status_code == 200, put.text
        assert put.json()["mode"] == mode
        assert put.json()["defaulted"] is False

        got = client.get("/v1/deployment", headers=headers()).json()
        assert got["mode"] == mode
        assert got["defaulted"] is False


def test_put_writes_file_on_disk(tmp_path):
    app = build_app(tmp_path)
    with TestClient(app) as client:
        client.put(
            "/v1/deployment", headers=headers(),
            json={"mode": "server", "server_url": "https://ling.example.com", "token": "tk"},
        )
    path = tmp_path / DEPLOYMENT_FILENAME
    assert path.is_file()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw == {
        "mode": "server",
        "server_url": "https://ling.example.com",
        "token": "tk",
    }


def test_token_may_be_empty(tmp_path):
    app = build_app(tmp_path)
    with TestClient(app) as client:
        response = client.put(
            "/v1/deployment", headers=headers(),
            json={"mode": "server", "server_url": "http://127.0.0.1:7100", "token": ""},
        )
        assert response.status_code == 200
        assert response.json()["token"] == ""


def test_put_returns_content_read_back_from_disk(tmp_path):
    """返回值是写完再读回来的真实内容，不是内存里那份。"""
    app = build_app(tmp_path)
    with TestClient(app) as client:
        body = client.put("/v1/deployment", headers=headers(), json={"mode": "terminal"})
        assert body.json() == {
            "mode": "terminal",
            "server_url": "",
            "token": "",
            "defaulted": False,
        }


# ---------------------------------------------------------------------------
# 3. 校验
# ---------------------------------------------------------------------------


def test_invalid_mode_rejected(tmp_path):
    app = build_app(tmp_path)
    with TestClient(app) as client:
        response = client.put("/v1/deployment", headers=headers(), json={"mode": "cloud"})
        assert response.status_code == 422


def test_server_mode_requires_url(tmp_path):
    app = build_app(tmp_path)
    with TestClient(app) as client:
        for url in ("", "   "):
            response = client.put(
                "/v1/deployment", headers=headers(),
                json={"mode": "server", "server_url": url},
            )
            assert response.status_code == 422, url


@pytest.mark.parametrize(
    "url", ["ftp://x/v1", "file:///etc/passwd", "127.0.0.1:7100", "javascript:alert(1)"]
)
def test_server_mode_requires_http_url(tmp_path, url):
    app = build_app(tmp_path)
    with TestClient(app) as client:
        response = client.put(
            "/v1/deployment", headers=headers(),
            json={"mode": "server", "server_url": url},
        )
        assert response.status_code == 422


def test_https_url_accepted(tmp_path):
    app = build_app(tmp_path)
    with TestClient(app) as client:
        response = client.put(
            "/v1/deployment", headers=headers(),
            json={"mode": "server", "server_url": "https://box.example.com:443"},
        )
        assert response.status_code == 200


def test_failed_put_does_not_overwrite_existing_file(tmp_path):
    """校验失败时磁盘上的旧配置必须原封不动。"""
    app = build_app(tmp_path)
    with TestClient(app) as client:
        client.put(
            "/v1/deployment", headers=headers(),
            json={"mode": "server", "server_url": "http://127.0.0.1:7100", "token": "old"},
        )
        bad = client.put(
            "/v1/deployment", headers=headers(),
            json={"mode": "server", "server_url": "ftp://nope"},
        )
        assert bad.status_code == 422
        got = client.get("/v1/deployment", headers=headers()).json()
        assert got["server_url"] == "http://127.0.0.1:7100"
        assert got["token"] == "old"


def test_partial_update_keeps_other_fields(tmp_path):
    """只传 mode 时，server_url/token 保持原值。"""
    app = build_app(tmp_path)
    with TestClient(app) as client:
        client.put(
            "/v1/deployment", headers=headers(),
            json={"mode": "server", "server_url": "http://127.0.0.1:7100", "token": "keep-me"},
        )
        body = client.put("/v1/deployment", headers=headers(), json={"mode": "local"}).json()
        assert body["mode"] == "local"
        assert body["server_url"] == "http://127.0.0.1:7100"
        assert body["token"] == "keep-me"


def test_normalize_rejects_bad_values():
    with pytest.raises(DeploymentError):
        normalize("nope", "", "")
    with pytest.raises(DeploymentError):
        normalize("server", "", "")
    with pytest.raises(DeploymentError):
        normalize("local", "ftp://x", "")


def test_validate_server_url_trims_whitespace():
    assert validate_server_url("  http://127.0.0.1:7100  ") == "http://127.0.0.1:7100"


# ---------------------------------------------------------------------------
# 4. 原子写
# ---------------------------------------------------------------------------


def test_atomic_write_leaves_no_temp_file(tmp_path):
    write_deployment(tmp_path, Deployment(mode="terminal"))
    assert not list(tmp_path.glob("*.tmp"))
    assert (tmp_path / DEPLOYMENT_FILENAME).is_file()


def test_atomic_write_replaces_content_wholesale(tmp_path):
    """先写长内容再写短内容：不能残留旧内容。"""
    write_deployment(
        tmp_path,
        Deployment(mode="server", server_url="http://" + "a" * 100 + ".com", token="x" * 100),
    )
    write_deployment(tmp_path, Deployment(mode="local"))
    raw = json.loads((tmp_path / DEPLOYMENT_FILENAME).read_text(encoding="utf-8"))
    assert raw == {"mode": "local", "server_url": "", "token": ""}


def test_concurrent_writes_never_leave_corrupt_json(tmp_path):
    """并发写 + 并发读：读到的永远是完整合法 JSON，不会读到半个文件。"""
    write_deployment(tmp_path, Deployment(mode="local"))
    errors: list[Exception] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                # 用真实读路径：它内部对 Windows 的瞬时占用有退避重试
                deployment = read_deployment(tmp_path)
                assert deployment.mode in MODES
                # 文件一定读得到，不该退化成默认值
                assert deployment.defaulted is False, deployment.reason
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                return

    def writer(mode):
        # server 模式必须带 server_url，否则写进去的就是非法配置
        url = "http://127.0.0.1:7100" if mode == "server" else ""
        try:
            for _ in range(40):
                write_deployment(
                    tmp_path, Deployment(mode=mode, server_url=url, token="t" * 50)
                )
        except Exception as exc:  # noqa: BLE001 - 收集起来在主线程断言
            errors.append(exc)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        workers = [
            threading.Thread(target=writer, args=(modes,))
            for modes in ("local", "terminal", "server")
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
    finally:
        stop.set()
        thread.join(timeout=5)

    assert not errors, f"并发读写出错：{errors[:1]}"
    assert not list(tmp_path.glob("*.tmp"))


# ---------------------------------------------------------------------------
# 5. 损坏文件降级（不假装读到真实配置）
# ---------------------------------------------------------------------------


def test_corrupt_json_falls_back_to_default(tmp_path):
    (tmp_path / DEPLOYMENT_FILENAME).write_text("{not json", encoding="utf-8")
    deployment = read_deployment(tmp_path)
    assert deployment.mode == "local"
    assert deployment.defaulted is True
    assert deployment.reason


def test_invalid_content_falls_back_to_default(tmp_path):
    (tmp_path / DEPLOYMENT_FILENAME).write_text(
        json.dumps({"mode": "cloud", "server_url": ""}), encoding="utf-8"
    )
    deployment = read_deployment(tmp_path)
    assert deployment.defaulted is True
    assert deployment.mode == "local"


def test_non_object_json_falls_back(tmp_path):
    (tmp_path / DEPLOYMENT_FILENAME).write_text("[1,2,3]", encoding="utf-8")
    assert read_deployment(tmp_path).defaulted is True


def test_server_mode_file_without_url_is_defaulted(tmp_path):
    (tmp_path / DEPLOYMENT_FILENAME).write_text(
        json.dumps({"mode": "server", "server_url": ""}), encoding="utf-8"
    )
    assert read_deployment(tmp_path).defaulted is True


# ---------------------------------------------------------------------------
# 6. 认证与路径
# ---------------------------------------------------------------------------


def test_requires_auth(tmp_path):
    with TestClient(build_app(tmp_path)) as client:
        assert client.get("/v1/deployment").status_code == 401
        assert client.put("/v1/deployment", json={"mode": "local"}).status_code == 401


def test_file_path_is_under_data_dir(tmp_path):
    assert deployment_path(tmp_path) == tmp_path / DEPLOYMENT_FILENAME
