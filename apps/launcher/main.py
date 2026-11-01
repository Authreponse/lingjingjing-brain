#!/usr/bin/env python3
"""Lingjingjing desktop launcher (Python 3.11+, standard library only)."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
import webbrowser

LOGGER = logging.getLogger("lingjingjing.launcher")
DEFAULT_PORT = 7100
DEFAULT_TOKEN = "lingjingjing-local-dev"
HEALTH_PATH = "/v1/health"


class LauncherError(RuntimeError):
    """An actionable startup or supervision failure."""


class RuntimeFormatError(LauncherError):
    """runtime.json does not follow docs/api-v4.md section 4."""


class ReadinessTimeout(LauncherError):
    """The runtime file and authenticated health check did not become ready."""


class BackendCrashed(LauncherError):
    """A child backend exited, or exited again after its single restart."""


class DeploymentError(LauncherError):
    """deployment.json is invalid or incomplete for the selected mode."""


@dataclass(frozen=True)
class DeploymentInfo:
    mode: str
    server_url: str = ""
    token: str = ""
    source: str = "deployment.json"


@dataclass(frozen=True)
class RuntimeInfo:
    port: int
    token: str
    pid: int | None
    started_at: str | None
    source: str = "runtime.json"
    base_url: str | None = None

    @property
    def brain_url(self) -> str:
        return self.base_url or f"http://127.0.0.1:{self.port}"


def project_root() -> Path:
    """The frozen executable is expected in the project root (see README)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def fallback_runtime() -> RuntimeInfo:
    return RuntimeInfo(DEFAULT_PORT, DEFAULT_TOKEN, None, None, source="默认地址（降级）")


def load_deployment(
    path: Path,
    *,
    environ: dict[str, str] | None = None,
    logger: logging.Logger = LOGGER,
) -> DeploymentInfo:
    """Read deployment mode; only a missing file defaults to local."""
    environment = os.environ if environ is None else environ
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        source = str(path)
    except FileNotFoundError:
        payload = {}
        source = "默认 local（配置缺失）"
        logger.warning("未找到 %s；按 local 部署模式处理", path)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeploymentError(f"deployment.json 无法读取: {exc}") from exc
    if not isinstance(payload, dict):
        raise DeploymentError("deployment.json 顶层必须是 JSON 对象")
    mode = payload.get("mode", "local")
    server_url = payload.get("server_url", "")
    token = payload.get("token", "")
    if not isinstance(server_url, str) or not isinstance(token, str):
        raise DeploymentError("deployment.json 的 server_url/token 必须是字符串")
    override = environment.get("LING_DEPLOY_MODE")
    if override is not None:
        mode = override.strip().lower()
        logger.info("使用 LING_DEPLOY_MODE 覆盖部署模式：%s", mode)
    if not isinstance(mode, str):
        raise DeploymentError("deployment.json 的 mode 必须是字符串")
    mode = mode.strip().lower()
    if mode not in {"local", "terminal", "server"}:
        raise DeploymentError(f"未知部署模式 {mode!r}；只支持 local/terminal/server")
    if mode == "server":
        server_url = server_url.strip().rstrip("/")
        try:
            parts = urlsplit(server_url)
            parts.port  # Force validation of malformed/non-numeric ports.
        except ValueError as exc:
            raise DeploymentError(f"server_url 无效: {exc}") from exc
        if parts.scheme not in {"http", "https"} or not parts.netloc or parts.query or parts.fragment:
            raise DeploymentError("server 模式要求 server_url 是不带 query/fragment 的 http(s) 地址")
        if parts.username is not None or parts.password is not None:
            raise DeploymentError("server_url 不得内嵌用户名或密码；请使用独立 token 字段")
        if not token:
            raise DeploymentError("server 模式要求非空 token")
    return DeploymentInfo(mode, server_url, token, source=source)


def load_runtime(path: Path) -> RuntimeInfo:
    """Parse exactly the handshake fields documented in V4 section 4."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeFormatError(f"runtime.json 无法读取: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeFormatError("runtime.json 顶层必须是 JSON 对象")
    try:
        port = payload["port"]
        token = payload["token"]
        pid = payload["pid"]
        started_at = payload["started_at"]
    except KeyError as exc:
        raise RuntimeFormatError(f"runtime.json 缺少字段: {exc.args[0]}") from exc
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise RuntimeFormatError("runtime.json 的 port 必须是 1-65535 的整数")
    if not isinstance(token, str) or not token:
        raise RuntimeFormatError("runtime.json 的 token 必须是非空字符串")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise RuntimeFormatError("runtime.json 的 pid 必须是正整数")
    if not isinstance(started_at, str) or not started_at:
        raise RuntimeFormatError("runtime.json 的 started_at 必须是非空 ISO 8601 字符串")
    try:
        parsed_start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeFormatError("runtime.json 的 started_at 不是有效 ISO 8601 时间") from exc
    if parsed_start.tzinfo is None or parsed_start.utcoffset() is None:
        raise RuntimeFormatError("runtime.json 的 started_at 必须带时区")
    return RuntimeInfo(port, token, pid, started_at)


def probe_health(brain_url: str, token: str, timeout: float = 1.0) -> bool:
    """Only HTTP 200 is healthy; 401/404/503 and connection errors are not."""
    request = Request(
        brain_url.rstrip("/") + HEALTH_PATH,
        headers={"Accept": "application/json", "X-Ling-Token": token},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except (HTTPError, URLError, OSError, TimeoutError):
        return False


def _process_exit_code(process: Any) -> int | None:
    return process.poll() if process is not None else None


def wait_for_ready(
    runtime_path: Path,
    *,
    log_path: Path,
    process: Any = None,
    timeout: float = 30.0,
    interval: float = 0.25,
    health_probe: Callable[[str, str], bool] = probe_health,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    logger: logging.Logger = LOGGER,
) -> RuntimeInfo:
    """Require runtime.json plus health 200, with an explicit missing-file fallback."""
    if timeout <= 0 or interval <= 0:
        raise ValueError("timeout 和 interval 必须大于 0")
    deadline = clock() + timeout
    degraded = False
    invalid_runtime = False
    last_detail = "尚未执行健康检查"
    while True:
        exit_code = _process_exit_code(process)
        if exit_code is not None:
            raise BackendCrashed(
                f"后端进程在就绪前退出（exit={exit_code}）。请查看日志 {log_path}；"
                f"同时检查 {runtime_path} 与端口是否被占用。"
            )
        info: RuntimeInfo | None = None
        try:
            candidate = load_runtime(runtime_path)
            child_pid = getattr(process, "pid", None) if process is not None else None
            if child_pid is not None and candidate.pid != child_pid:
                last_detail = f"runtime.json pid={candidate.pid} 与当前后端 pid={child_pid} 不一致（可能是旧文件）"
            else:
                info = candidate
                last_detail = f"{candidate.brain_url}{HEALTH_PATH} 尚未返回 200"
        except FileNotFoundError:
            if not degraded:
                logger.warning("未找到 %s；降级使用 127.0.0.1:7100 和开发令牌轮询健康接口", runtime_path)
                degraded = True
            info = fallback_runtime()
            last_detail = "runtime.json 缺失，默认地址健康检查尚未返回 200"
        except RuntimeFormatError as exc:
            if not invalid_runtime:
                logger.error("%s；明确降级使用默认地址与开发令牌，不会把该错误隐藏为成功", exc)
                invalid_runtime = True
            info = fallback_runtime()
            last_detail = str(exc)

        if info is not None:
            try:
                healthy = health_probe(info.brain_url, info.token)
            except (OSError, ValueError) as exc:
                healthy = False
                last_detail = f"健康检查异常: {exc}"
            if healthy:
                if info.source != "runtime.json":
                    logger.warning("后端通过默认地址就绪；这是 runtime.json 缺失/无效时的降级结果")
                return info

        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleep(min(interval, remaining))
    raise ReadinessTimeout(
        f"等待后端就绪超时（{timeout:g}s）。最后状态：{last_detail}。"
        f"诊断：检查 {runtime_path}、{log_path}、端口占用及 X-Ling-Token 是否一致。"
    )


def discover_existing(
    runtime_path: Path,
    *,
    health_probe: Callable[[str, str], bool] = probe_health,
    logger: logging.Logger = LOGGER,
) -> RuntimeInfo | None:
    """Attach only to an already healthy backend; never take ownership of it."""
    try:
        info = load_runtime(runtime_path)
    except FileNotFoundError:
        logger.warning("检测已有后端时未找到 %s；尝试默认地址，若需启动也会继续记录降级", runtime_path)
        info = fallback_runtime()
    except RuntimeFormatError as exc:
        logger.warning("已有 runtime.json 无效（%s）；尝试默认地址", exc)
        info = fallback_runtime()
    if health_probe(info.brain_url, info.token):
        logger.info("检测到已运行的健康后端：%s（%s）", info.brain_url, info.source)
        return info
    return None


def spawn_backend(root: Path, data_dir: Path, python: str, log_path: Path) -> subprocess.Popen:
    """Start python -m lingbrain as an independent, logged child process."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    brain_src = root / "apps" / "brain" / "src"
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(brain_src) + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
    env["LING_DATA_DIR"] = str(data_dir)
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
    else:
        kwargs["start_new_session"] = True
    with log_path.open("a", encoding="utf-8") as log_stream:
        process = subprocess.Popen(
            [python, "-m", "lingbrain"],
            cwd=str(root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
    LOGGER.info("已拉起后端进程 pid=%s；日志：%s", process.pid, log_path)
    return process


class BackendSupervisor:
    def __init__(
        self,
        root: Path,
        data_dir: Path,
        *,
        python: str,
        timeout: float = 30.0,
        logger: logging.Logger = LOGGER,
    ) -> None:
        self.root = root
        self.data_dir = data_dir
        self.python = python
        self.timeout = timeout
        self.logger = logger
        self.runtime_path = data_dir / "runtime.json"
        self.log_path = data_dir / "logs" / "brain.log"
        self.process: subprocess.Popen | None = None
        self.restarts_used = 0
        self.info: RuntimeInfo | None = None

    def start(self) -> None:
        self.process = spawn_backend(self.root, self.data_dir, self.python, self.log_path)

    def ensure_ready(self) -> RuntimeInfo:
        existing = discover_existing(self.runtime_path, logger=self.logger)
        if existing is not None:
            self.info = existing
            return existing
        self.start()
        self.info = wait_for_ready(
            self.runtime_path,
            log_path=self.log_path,
            process=self.process,
            timeout=self.timeout,
            logger=self.logger,
        )
        return self.info

    def monitor(self) -> None:
        """Restart an owned crashed child once; a second exit is fatal."""
        if self.process is None:
            return
        exit_code = self.process.poll()
        if exit_code is None:
            return
        if self.restarts_used >= 1:
            raise BackendCrashed(
                f"后端第二次退出（exit={exit_code}），自动重启额度已用完。日志：{self.log_path}"
            )
        self.restarts_used += 1
        self.logger.warning("检测到后端退出（exit=%s）；执行仅有的一次自动重启", exit_code)
        self.start()
        try:
            self.info = wait_for_ready(
                self.runtime_path,
                log_path=self.log_path,
                process=self.process,
                timeout=self.timeout,
                logger=self.logger,
            )
        except LauncherError as exc:
            raise BackendCrashed(f"后端自动重启后未能恢复：{exc}") from exc
        self.logger.warning("后端自动重启并恢复健康检查；本次重启不会再次自动重启")

    def stop(self) -> None:
        """Stop only a process spawned by this launcher, never an adopted one."""
        process = self.process
        if process is None or process.poll() is not None:
            return
        self.logger.info("--stop-backend：终止本次 launcher 拉起的后端 pid=%s", process.pid)
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.logger.warning("后端未在 5 秒内退出，执行强制终止")
            process.kill()
            process.wait(timeout=5)


def connect_backend(
    supervisor: BackendSupervisor,
    deployment: DeploymentInfo,
    *,
    health_probe: Callable[[str, str], bool] = probe_health,
    logger: logging.Logger = LOGGER,
) -> RuntimeInfo:
    """Select a backend without silently spawning one in terminal/server mode."""
    if deployment.mode == "local":
        return supervisor.ensure_ready()
    if deployment.mode == "terminal":
        info = fallback_runtime()
        if not health_probe(info.brain_url, info.token):
            raise LauncherError("请先启动后端：terminal 模式探测 127.0.0.1:7100 /v1/health 未就绪")
        logger.info("terminal 模式：本机后端已就绪，launcher 不接管其进程")
        return RuntimeInfo(info.port, info.token, None, None, source="terminal 本机后端")
    parts = urlsplit(deployment.server_url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    logger.info("server 模式：不拉起或探测本地后端，前端直连 %s", deployment.server_url)
    return RuntimeInfo(
        port,
        deployment.token,
        None,
        None,
        source="deployment.json(server)",
        base_url=deployment.server_url,
    )


def build_injection_url(base_url: str, brain_url: str, token: str) -> str:
    """Inject V4 query keys without dropping existing query/fragment data."""
    parts = urlsplit(base_url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key not in {"brain_url", "token"}]
    query.extend((("brain_url", brain_url), ("token", token)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        # In particular, never write the token-bearing query URL to stdout.
        return


@dataclass
class FrontendHandle:
    kind: str
    url: str
    process: subprocess.Popen | None = None
    server: ThreadingHTTPServer | None = None
    thread: threading.Thread | None = None

    def close(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)


def start_static_server(dist: Path) -> FrontendHandle:
    if not (dist / "index.html").is_file():
        raise LauncherError(f"前端 dist 缺少 index.html：{dist}")
    handler = partial(QuietStaticHandler, directory=str(dist))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, name="lingjingjing-static", daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/"
    LOGGER.info("前端 dist 静态服务：%s（目录 %s）", url, dist)
    return FrontendHandle("browser-dist", url, server=server, thread=thread)


def find_tauri_executable(shell_dir: Path) -> Path | None:
    release = shell_dir / "src-tauri" / "target" / "release"
    names = ("灵鲸鲸.exe", "lingjingjing-shell.exe", "lingjingjing.exe") if os.name == "nt" else (
        "灵鲸鲸",
        "lingjingjing-shell",
    )
    for name in names:
        candidate = release / name
        if candidate.is_file():
            return candidate
    if os.name == "nt" and release.is_dir():
        # Only the release root: NSIS/MSI bundle executables are installers.
        return next((path for path in sorted(release.glob("*.exe")) if path.is_file()), None)
    return None


def launch_frontend(
    root: Path,
    data_dir: Path,
    info: RuntimeInfo,
    *,
    opener: Callable[[str], bool] = webbrowser.open,
) -> FrontendHandle:
    shell_dir = root / "apps" / "shell"
    executable = find_tauri_executable(shell_dir)
    if executable is not None:
        env = os.environ.copy()
        env["LING_BRAIN_URL"] = info.brain_url
        env["LING_BRAIN_TOKEN"] = info.token
        log_path = data_dir / "logs" / "shell.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_stream:
            process = subprocess.Popen(
                [str(executable)],
                cwd=str(executable.parent),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
            )
        LOGGER.info("已启动 Tauri 前端：%s", executable)
        return FrontendHandle("tauri", executable.name, process=process)

    dist = shell_dir / "dist"
    if dist.is_dir():
        handle = start_static_server(dist)
        handle.url = build_injection_url(handle.url, info.brain_url, info.token)
        if not opener(handle.url):
            handle.close()
            raise LauncherError("系统默认浏览器拒绝打开前端 URL；后端状态未被伪报为前端成功")
        return handle

    # Original V4 development fallback when no built frontend exists.
    url = build_injection_url("http://127.0.0.1:1420/", info.brain_url, info.token)
    LOGGER.warning("未找到 Tauri 产物或 %s；降级打开 127.0.0.1:1420（需前端开发服务器）", dist)
    if not opener(url):
        raise LauncherError("默认浏览器打开失败，且没有可用 Tauri/dist 前端产物")
    return FrontendHandle("browser-dev", url)


def configure_logging(data_dir: Path) -> None:
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    file_handler = logging.FileHandler(log_dir / "launcher.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    LOGGER.handlers.clear()
    LOGGER.addHandler(stream)
    LOGGER.addHandler(file_handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="灵鲸鲸桌面 launcher：后端握手、前端启动与一次崩溃自愈")
    parser.add_argument("--root", type=Path, default=project_root(), help="项目根目录（默认按脚本位置推导）")
    parser.add_argument("--data-dir", type=Path, help="默认 <root>/data")
    parser.add_argument("--python", default=sys.executable, help="用于 python -m lingbrain 的解释器")
    parser.add_argument("--timeout", type=float, default=30.0, help="后端就绪超时秒数（默认 30）")
    parser.add_argument("--stop-backend", action="store_true", help="退出时终止本次拉起的后端；默认保留")
    return parser


def run(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    data_dir = (args.data_dir or root / "data").resolve()
    configure_logging(data_dir)
    supervisor = BackendSupervisor(root, data_dir, python=args.python, timeout=args.timeout)
    frontend: FrontendHandle | None = None
    try:
        deployment = load_deployment(data_dir / "deployment.json")
        info = connect_backend(supervisor, deployment)
        frontend = launch_frontend(root, data_dir, info)
        LOGGER.info("前端启动完成（部署模式 %s）", deployment.mode)
        while True:
            supervisor.monitor()
            if frontend.process is not None and frontend.process.poll() is not None:
                LOGGER.info("Tauri 前端已退出，launcher 结束")
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        LOGGER.info("收到退出请求")
    finally:
        if frontend is not None:
            frontend.close()
        if args.stop_backend:
            supervisor.stop()
        elif supervisor.process is not None:
            LOGGER.info("保留后端运行（未指定 --stop-backend）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    try:
        return run(args)
    except (LauncherError, OSError) as exc:
        LOGGER.error("launcher 失败：%s", exc)
        if not LOGGER.handlers:
            print(f"launcher 失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
