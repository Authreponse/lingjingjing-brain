"""部署模式持久化：读写 data/deployment.json。

为什么需要这个端点
------------------
前端（Tauri webview）写不了 data/ 目录，launcher 只读文件不写 ——
所以"前端 ↔ 磁盘"之间需要一个唯一的写入方，就是这里。

文件形状::

    {"mode": "local" | "terminal" | "server", "server_url": "...", "token": "..."}

校验规则
--------
* ``mode`` 必须是三选一。
* ``server`` 模式下 ``server_url`` 必填，且必须是合法的 http(s) 地址。
* 非 server 模式下 server_url 允许为空（保留原值也能写回，不做无谓清空）。
* ``token`` 允许为空字符串。

写入安全
--------
**原子写**：先写同目录临时文件，再 ``os.replace`` 覆盖。
launcher 任何时刻读到的要么是旧的完整内容，要么是新的完整内容，
不会读到写了一半的 JSON。文件里有令牌，因此权限收成 0600。

读取行为
--------
文件不存在、内容损坏、或字段不合法时，**返回默认值并标记 ``defaulted: true``**，
同时说明原因——不静默假装读到了一份真实配置。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

#: 部署模式枚举
MODES = ("local", "terminal", "server")
#: 默认配置（文件缺失时返回这个）
DEFAULT_MODE = "local"
DEPLOYMENT_FILENAME = "deployment.json"
#: 字段长度上限，避免把超长串写进文件
MAX_URL_LENGTH = 500
MAX_TOKEN_LENGTH = 500


class DeploymentError(ValueError):
    """配置不合法。路由层映射 422。"""


def deployment_path(data_dir: Path) -> Path:
    return Path(data_dir) / DEPLOYMENT_FILENAME


@dataclass(slots=True)
class Deployment:
    mode: str = DEFAULT_MODE
    server_url: str = ""
    token: str = ""
    #: 本次读取是否用了默认值（文件不存在/损坏）
    defaulted: bool = False
    #: defaulted 的原因，给人看
    reason: str | None = None

    def out(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "mode": self.mode,
            "server_url": self.server_url,
            "token": self.token,
            "defaulted": self.defaulted,
        }
        if self.reason:
            payload["reason"] = self.reason
        return payload


def validate_server_url(url: str) -> str:
    """校验 server 模式的地址。返回规范化后的字符串。"""
    text = (url or "").strip()
    if not text:
        raise DeploymentError("server 模式必须填写 server_url")
    if len(text) > MAX_URL_LENGTH:
        raise DeploymentError(f"server_url 太长（上限 {MAX_URL_LENGTH} 字符）")
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"}:
        raise DeploymentError("server_url 必须以 http:// 或 https:// 开头")
    if not parts.hostname:
        raise DeploymentError("server_url 缺少主机名")
    return text


def validate_mode(mode: str) -> str:
    text = (mode or "").strip().lower()
    if text not in MODES:
        raise DeploymentError(f"mode 只能是 {' / '.join(MODES)}")
    return text


def normalize(mode: str, server_url: str, token: str) -> Deployment:
    """把一组输入校验并规范化成 Deployment。"""
    mode = validate_mode(mode)
    server_url = (server_url or "").strip()
    token = (token or "").strip()
    if len(token) > MAX_TOKEN_LENGTH:
        raise DeploymentError(f"token 太长（上限 {MAX_TOKEN_LENGTH} 字符）")
    if mode == "server":
        server_url = validate_server_url(server_url)
    elif server_url and not server_url.startswith(("http://", "https://")):
        # 非 server 模式允许留空；但填了就得是合法地址，免得切换模式时才炸
        raise DeploymentError("server_url 必须以 http:// 或 https:// 开头")
    return Deployment(mode=mode, server_url=server_url, token=token)


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------


def _read_text_with_retry(path: Path) -> str:
    """读文件，遇到 Windows 瞬时占用重试几次。

    Windows 上 ``os.replace`` 期间目标文件可能被短暂独占，
    这时读会抛 PermissionError —— 那不是"文件损坏"，退避重读即可。
    """
    last: OSError | None = None
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError as exc:  # pragma: no cover - 取决于系统时序
            last = exc
            time.sleep(_REPLACE_BACKOFF * (attempt + 1))
        except OSError:
            raise
    assert last is not None
    raise last


def read_deployment(data_dir: Path) -> Deployment:
    """读配置。文件缺失/损坏 → 默认值 + defaulted=True（并说明原因）。"""
    path = deployment_path(data_dir)
    if not path.is_file():
        return Deployment(defaulted=True, reason="还没有保存过部署配置，使用默认值")

    try:
        raw = json.loads(_read_text_with_retry(path))
    except (OSError, ValueError) as exc:
        logger.warning("deployment.json 读不了，按默认值处理：%s", exc)
        return Deployment(defaulted=True, reason=f"配置文件损坏：{exc}")

    if not isinstance(raw, dict):
        return Deployment(defaulted=True, reason="配置文件格式不对（不是 JSON 对象）")

    try:
        deployment = normalize(
            raw.get("mode", DEFAULT_MODE),
            raw.get("server_url", "") or "",
            raw.get("token", "") or "",
        )
    except DeploymentError as exc:
        # 文件存在但内容不合法：不假装它是有效配置
        logger.warning("deployment.json 内容不合法，按默认值处理：%s", exc)
        return Deployment(defaulted=True, reason=f"配置文件内容不合法：{exc}")
    return deployment


#: 同一个进程内串行化写入：FastAPI 默认线程池里多个请求可能同时 PUT。
_WRITE_LOCK = threading.Lock()
#: Windows 上 replace 偶尔会被正在读文件的进程短暂占用，重试几次即可。
_REPLACE_ATTEMPTS = 5
_REPLACE_BACKOFF = 0.02


def _replace_with_retry(tmp: Path, path: Path) -> None:
    """os.replace 在 Windows 上可能因目标被短暂打开而 WinError 32/5，重试。"""
    last: OSError | None = None
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except OSError as exc:  # pragma: no cover - 取决于操作系统时序
            last = exc
            if attempt == _REPLACE_ATTEMPTS - 1:
                break
            time.sleep(_REPLACE_BACKOFF * (attempt + 1))
    assert last is not None
    raise last


def write_deployment(data_dir: Path, deployment: Deployment) -> Path:
    """原子写：临时文件 + os.replace，避免 launcher 读到半个文件。

    并发安全要点：
    * 进程内用锁串行化，避免多个请求抢同一个临时文件；
    * 临时文件名带 pid+随机后缀，即使跨进程也不会互相覆盖；
    * replace 在 Windows 上可能被短暂占用，做有限次重试。
    """
    path = deployment_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "mode": deployment.mode,
        "server_url": deployment.server_url,
        "token": deployment.token,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    with _WRITE_LOCK:
        # 唯一临时文件名：同进程多线程 / 跨进程都不会撞车
        tmp = path.with_name(
            f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        )
        try:
            tmp.write_text(text, encoding="utf-8")
            _replace_with_retry(tmp, path)
        except OSError:
            # 写失败要把临时文件清掉，别在 data/ 里留垃圾
            try:
                tmp.unlink(missing_ok=True)
            except OSError:  # pragma: no cover
                pass
            raise
    try:
        os.chmod(path, 0o600)  # 里面有令牌，只给当前用户读
    except OSError:  # pragma: no cover - Windows 上可能不支持
        pass
    return path


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class DeploymentUpdateRequest(BaseModel):
    """PUT /v1/deployment 的请求体。三个字段都可省略（省略即保持原值）。"""

    mode: str | None = Field(default=None, max_length=20)
    server_url: str | None = Field(default=None, max_length=MAX_URL_LENGTH)
    token: str | None = Field(default=None, max_length=MAX_TOKEN_LENGTH)

    @field_validator("mode")
    @classmethod
    def _mode(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value.strip().lower() not in MODES:
            raise ValueError(f"mode 只能是 {' / '.join(MODES)}")
        return value.strip().lower()


def create_deployment_router(
    data_dir: Path,
    *,
    authorize: Callable[..., Any] | None = None,
) -> APIRouter:
    """GET/PUT /v1/deployment。authorize 传 app.py 里的认证依赖。"""
    dependencies = [Depends(authorize)] if authorize is not None else []
    router = APIRouter(dependencies=dependencies, tags=["deployment"])

    @router.get("/v1/deployment")
    async def get_deployment() -> dict:
        return read_deployment(data_dir).out()

    @router.put("/v1/deployment")
    async def put_deployment(request: DeploymentUpdateRequest) -> dict:
        current = read_deployment(data_dir)
        sent = request.model_fields_set
        mode = request.mode if "mode" in sent else current.mode
        server_url = request.server_url if "server_url" in sent else current.server_url
        token = request.token if "token" in sent else current.token
        try:
            deployment = normalize(mode, server_url, token)
        except DeploymentError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            write_deployment(data_dir, deployment)
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"写入 deployment.json 失败：{exc}",
            ) from exc
        # 写完再读回来，返回磁盘上的真实内容（而不是我以为写进去的内容）
        return read_deployment(data_dir).out()

    return router


__all__ = [
    "Deployment",
    "DeploymentError",
    "DeploymentUpdateRequest",
    "create_deployment_router",
    "deployment_path",
    "read_deployment",
    "write_deployment",
    "normalize",
    "validate_mode",
    "validate_server_url",
    "MODES",
    "DEFAULT_MODE",
    "DEPLOYMENT_FILENAME",
]
