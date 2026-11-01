from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from apps.launcher import main as launcher


class FakeProcess:
    def __init__(self, pid: int = 1234, exit_code: int | None = None) -> None:
        self.pid = pid
        self.exit_code = exit_code
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True
        self.exit_code = -15

    def kill(self) -> None:
        self.killed = True
        self.exit_code = -9

    def wait(self, timeout: float | None = None) -> int:
        assert self.exit_code is not None
        return self.exit_code


def write_runtime(path: Path, *, port: int = 7101, token: str = "secret", pid: int = 1234) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"port": port, "token": token, "pid": pid, "started_at": "2026-11-01T10:00:00+00:00"}),
        encoding="utf-8",
    )


def test_runtime_parsing_follows_v4(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    write_runtime(path)
    info = launcher.load_runtime(path)
    assert info.port == 7101
    assert info.token == "secret"
    assert info.pid == 1234
    assert info.brain_url == "http://127.0.0.1:7101"
    assert info.source == "runtime.json"


@pytest.mark.parametrize(
    "patch",
    [
        {"port": 0},
        {"port": True},
        {"token": ""},
        {"pid": -1},
        {"started_at": ""},
        {"started_at": "2026-11-01 10:00:00"},
        {"started_at": "not-a-time"},
    ],
)
def test_runtime_parsing_rejects_invalid_fields(tmp_path: Path, patch: dict) -> None:
    path = tmp_path / "runtime.json"
    write_runtime(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(patch)
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(launcher.RuntimeFormatError):
        launcher.load_runtime(path)


def test_wait_ready_requires_runtime_and_health_200(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.json"
    process = FakeProcess()
    now = [0.0]
    probes: list[tuple[str, str]] = []

    def sleep(seconds: float) -> None:
        now[0] += seconds
        if now[0] >= 0.2 and not runtime.exists():
            write_runtime(runtime)

    def healthy(url: str, token: str) -> bool:
        probes.append((url, token))
        return runtime.exists() and url == "http://127.0.0.1:7101" and token == "secret" and now[0] >= 0.3

    info = launcher.wait_for_ready(
        runtime,
        log_path=tmp_path / "logs" / "brain.log",
        process=process,
        timeout=1,
        interval=0.1,
        health_probe=healthy,
        sleep=sleep,
        clock=lambda: now[0],
        logger=Mock(spec=logging.Logger),
    )
    assert info.source == "runtime.json"
    assert probes[-1] == ("http://127.0.0.1:7101", "secret")


def test_missing_runtime_uses_logged_default_fallback(tmp_path: Path) -> None:
    logger = Mock(spec=logging.Logger)
    info = launcher.wait_for_ready(
        tmp_path / "runtime.json",
        log_path=tmp_path / "brain.log",
        process=FakeProcess(),
        timeout=1,
        interval=0.1,
        health_probe=lambda url, token: url == "http://127.0.0.1:7100" and token == launcher.DEFAULT_TOKEN,
        logger=logger,
    )
    assert info.source != "runtime.json"
    assert any("降级" in str(call) for call in logger.warning.call_args_list)


def test_ready_timeout_contains_actionable_diagnostics(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.json"
    log_path = tmp_path / "logs" / "brain.log"
    write_runtime(runtime)
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    with pytest.raises(launcher.ReadinessTimeout) as caught:
        launcher.wait_for_ready(
            runtime,
            log_path=log_path,
            process=FakeProcess(),
            timeout=0.5,
            interval=0.2,
            health_probe=lambda url, token: False,
            sleep=sleep,
            clock=lambda: now[0],
        )
    message = str(caught.value)
    assert "超时" in message
    assert str(runtime) in message
    assert str(log_path) in message
    assert "X-Ling-Token" in message


def test_fake_backend_exit_during_polling_is_not_a_timeout(tmp_path: Path) -> None:
    with pytest.raises(launcher.BackendCrashed) as caught:
        launcher.wait_for_ready(
            tmp_path / "runtime.json",
            log_path=tmp_path / "brain.log",
            process=FakeProcess(exit_code=7),
            health_probe=lambda url, token: pytest.fail("进程退出后不应继续请求健康检查"),
        )
    assert "exit=7" in str(caught.value)


def test_injection_url_preserves_existing_query_and_fragment() -> None:
    url = launcher.build_injection_url(
        "http://127.0.0.1:32123/?view=pet&token=old#chat",
        "http://127.0.0.1:7100",
        "令牌 +/&",
    )
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert query["view"] == ["pet"]
    assert query["brain_url"] == ["http://127.0.0.1:7100"]
    assert query["token"] == ["令牌 +/&"]
    assert parts.fragment == "chat"


def test_dist_frontend_uses_random_static_url_without_real_network(tmp_path: Path, monkeypatch) -> None:
    dist = tmp_path / "apps" / "shell" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("ok", encoding="utf-8")
    handle = launcher.FrontendHandle("browser-dist", "http://127.0.0.1:32123/")
    monkeypatch.setattr(launcher, "start_static_server", lambda path: handle)
    opened: list[str] = []
    result = launcher.launch_frontend(
        tmp_path,
        tmp_path / "data",
        launcher.RuntimeInfo(7100, "secret", 1, "now"),
        opener=lambda url: opened.append(url) or True,
    )
    assert result is handle
    query = parse_qs(urlsplit(opened[0]).query)
    assert query == {"brain_url": ["http://127.0.0.1:7100"], "token": ["secret"]}


def test_owned_backend_restarts_exactly_once(tmp_path: Path, monkeypatch) -> None:
    supervisor = launcher.BackendSupervisor(tmp_path, tmp_path / "data", python="fake-python")
    first = FakeProcess(pid=1, exit_code=1)
    second = FakeProcess(pid=2)
    supervisor.process = first
    monkeypatch.setattr(supervisor, "start", lambda: setattr(supervisor, "process", second))
    monkeypatch.setattr(
        launcher,
        "wait_for_ready",
        lambda *args, **kwargs: launcher.RuntimeInfo(7100, "secret", 2, "now"),
    )
    supervisor.monitor()
    assert supervisor.restarts_used == 1
    assert supervisor.process is second
    second.exit_code = 2
    with pytest.raises(launcher.BackendCrashed, match="第二次"):
        supervisor.monitor()


def test_stop_backend_terminates_only_owned_child(tmp_path: Path) -> None:
    supervisor = launcher.BackendSupervisor(tmp_path, tmp_path / "data", python="fake-python")
    process = FakeProcess()
    supervisor.process = process
    supervisor.stop()
    assert process.terminated
    assert process.exit_code == -15
    adopted = launcher.BackendSupervisor(tmp_path, tmp_path / "data", python="fake-python")
    adopted.stop()  # process=None means an existing backend was only discovered.


def test_existing_backend_is_adopted_not_spawned(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.json"
    write_runtime(runtime)
    info = launcher.discover_existing(runtime, health_probe=lambda url, token: True)
    assert info is not None
    assert info.pid == 1234


def test_missing_deployment_defaults_to_local_with_log(tmp_path: Path) -> None:
    logger = Mock(spec=logging.Logger)
    info = launcher.load_deployment(tmp_path / "deployment.json", environ={}, logger=logger)
    assert info.mode == "local"
    assert any("配置" in str(call) or "local" in str(call) for call in logger.warning.call_args_list)


def test_deployment_mode_environment_override(tmp_path: Path) -> None:
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps({"mode": "local", "server_url": "", "token": ""}), encoding="utf-8")
    info = launcher.load_deployment(path, environ={"LING_DEPLOY_MODE": "TERMINAL"})
    assert info.mode == "terminal"


def test_terminal_mode_probes_without_spawning(tmp_path: Path) -> None:
    supervisor = Mock(spec=launcher.BackendSupervisor)
    info = launcher.connect_backend(
        supervisor,
        launcher.DeploymentInfo("terminal"),
        health_probe=lambda url, token: url == "http://127.0.0.1:7100" and token == launcher.DEFAULT_TOKEN,
    )
    assert info.brain_url == "http://127.0.0.1:7100"
    supervisor.ensure_ready.assert_not_called()


def test_terminal_mode_unready_has_explicit_action(tmp_path: Path) -> None:
    supervisor = Mock(spec=launcher.BackendSupervisor)
    with pytest.raises(launcher.LauncherError, match="请先启动后端"):
        launcher.connect_backend(
            supervisor,
            launcher.DeploymentInfo("terminal"),
            health_probe=lambda url, token: False,
        )
    supervisor.ensure_ready.assert_not_called()


def test_server_mode_injects_remote_without_local_probe(tmp_path: Path) -> None:
    supervisor = Mock(spec=launcher.BackendSupervisor)
    deployment = launcher.DeploymentInfo("server", "https://brain.example.com/api", "remote-secret")
    info = launcher.connect_backend(
        supervisor,
        deployment,
        health_probe=lambda url, token: pytest.fail("server 模式不应探测本地后端"),
    )
    assert info.brain_url == "https://brain.example.com/api"
    assert info.token == "remote-secret"
    assert info.port == 443
    supervisor.ensure_ready.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "unknown", "server_url": "", "token": ""},
        {"mode": "server", "server_url": "", "token": "secret"},
        {"mode": "server", "server_url": "https://brain.example.com", "token": ""},
        {"mode": "server", "server_url": "file:///tmp/brain", "token": "secret"},
    ],
)
def test_invalid_deployment_is_not_silently_downgraded(tmp_path: Path, payload: dict) -> None:
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(launcher.DeploymentError):
        launcher.load_deployment(path, environ={})
