"""V4 提供商注册表与路由配置 HTTP 路由。

按 docs/api-v4.md 第 2 节实现，**不改契约**：

    GET    /v1/providers                  列表 + 当前路由 + limits
    POST   /v1/providers                  新建（api_key 只写不读）
    PUT    /v1/providers/{id}             更新（api_key: 省略/""=不改, null=清除）
    DELETE /v1/providers/{id}             删除（内置 echo 403）
    POST   /v1/providers/{id}/test        连通性测试（8s 超时）
    POST   /v1/providers/routing          保存路由配置

错误约定与既有一致：401 认证 / 404 不存在 / 422 校验失败（detail 可为字符串或
{"error","problems"}）/ 503 上游不可用。
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from .providers import (
    CAPABILITIES,
    KINDS,
    MAX_ENABLED,
    MODES,
    ProviderError,
    ProviderRegistry,
)


class ProviderCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    kind: str
    base_url: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=120)
    api_key: str | None = Field(default=None, max_length=500)
    capabilities: list[str] | None = None
    enabled: bool = True

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        if value.strip().lower() not in KINDS:
            raise ValueError(f"kind 只能是 {' / '.join(KINDS)}")
        return value.strip().lower()

    @field_validator("capabilities")
    @classmethod
    def _caps(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        for item in value:
            if item not in CAPABILITIES:
                raise ValueError(f"capabilities 只能是 {', '.join(CAPABILITIES)}")
        return value


class ProviderUpdateRequest(BaseModel):
    """更新：字段全部可选。

    ``api_key`` 的默认值是 **"省略"**（不是 None），用于区分"没传"与"传 null 清除"。
    """

    name: str | None = Field(default=None, min_length=1, max_length=40)
    kind: str | None = None
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=120)
    # 用 pydantic 的 model_fields_set 判断"有没有传"，而不是靠默认值
    api_key: str | None = None
    capabilities: list[str] | None = None
    enabled: bool | None = None

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value.strip().lower() not in KINDS:
            raise ValueError(f"kind 只能是 {' / '.join(KINDS)}")
        return value.strip().lower()

    @field_validator("capabilities")
    @classmethod
    def _caps(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        for item in value:
            if item not in CAPABILITIES:
                raise ValueError(f"capabilities 只能是 {', '.join(CAPABILITIES)}")
        return value


class RoutingRequest(BaseModel):
    mode: str
    active_provider_id: str | None = Field(default=None, max_length=80)
    enabled_ids: list[str] | None = None

    @field_validator("mode")
    @classmethod
    def _mode(cls, value: str) -> str:
        if value.strip().lower() not in MODES:
            raise ValueError("mode 只能是 single 或 concurrent")
        return value.strip().lower()


def _problem(code: str, problems: list[str]) -> dict[str, Any]:
    """契约允许 422 的 detail 是 {"error","problems"}。"""
    return {"error": code, "problems": list(problems)}


def _raise(exc: ProviderError) -> None:
    if isinstance(exc, ProviderError) and exc.problems:
        detail = _problem(exc.code, exc.problems)
    else:
        detail = str(exc)
    raise HTTPException(status_code=exc.http_status, detail=detail) from exc


def create_provider_router(
    registry: ProviderRegistry,
    *,
    authorize: Callable[..., Any] | None = None,
) -> APIRouter:
    """构造路由。``authorize`` 传 app.py 里的认证依赖。"""
    dependencies = [Depends(authorize)] if authorize is not None else []
    router = APIRouter(dependencies=dependencies, tags=["providers"])

    def _snapshot(warnings: list[str] | None = None) -> dict[str, Any]:
        providers = registry.list()
        payload: dict[str, Any] = {
            "items": [item.out() for item in providers],
            "routing": registry.routing().out(),
            "limits": {"max_enabled": MAX_ENABLED, "max_providers": 20},
        }
        if warnings:
            payload["warnings"] = list(warnings)
        return payload

    @router.get("/v1/providers")
    async def list_providers() -> dict:
        return _snapshot()

    # 注意：这条必须注册在 /v1/providers/{provider_id} 系列之前，
    # 否则 "routing" 会被当成一个 provider_id 匹配走。
    @router.post("/v1/providers/routing")
    async def set_routing(request: RoutingRequest) -> dict:
        try:
            registry.set_routing(
                mode=request.mode,
                active_provider_id=request.active_provider_id,
                enabled_ids=request.enabled_ids,
            )
        except ProviderError as exc:
            _raise(exc)
            raise  # pragma: no cover
        return _snapshot()

    @router.post("/v1/providers", status_code=201)
    async def create_provider(request: ProviderCreateRequest) -> dict:
        try:
            provider, warnings = registry.create(
                name=request.name,
                kind=request.kind,
                base_url=request.base_url,
                model=request.model,
                api_key=request.api_key or "",
                capabilities=request.capabilities,
                enabled=request.enabled,
            )
        except ProviderError as exc:
            _raise(exc)
            raise  # pragma: no cover
        snapshot = _snapshot(warnings)
        snapshot["created"] = provider.out()
        return snapshot

    @router.put("/v1/providers/{provider_id}")
    async def update_provider(provider_id: str, request: ProviderUpdateRequest) -> dict:
        sent = request.model_fields_set
        try:
            provider, warnings = registry.update(
                provider_id,
                name=request.name if "name" in sent else None,
                kind=request.kind if "kind" in sent else None,
                base_url=request.base_url if "base_url" in sent else None,
                model=request.model if "model" in sent else None,
                # 没传 = 不改(...)；传 null = 清除(None)；传非空 = 覆盖
                api_key=(request.api_key if "api_key" in sent else ...),
                capabilities=request.capabilities if "capabilities" in sent else None,
                enabled=request.enabled if "enabled" in sent else None,
            )
        except ProviderError as exc:
            _raise(exc)
            raise  # pragma: no cover
        snapshot = _snapshot(warnings)
        snapshot["updated"] = provider.out()
        return snapshot

    @router.delete("/v1/providers/{provider_id}")
    async def delete_provider(provider_id: str) -> dict:
        try:
            registry.delete(provider_id)
        except ProviderError as exc:
            _raise(exc)
            raise  # pragma: no cover
        return _snapshot()

    @router.post("/v1/providers/{provider_id}/test")
    async def test_provider(provider_id: str) -> dict:
        try:
            return await registry.test(provider_id)
        except ProviderError as exc:
            _raise(exc)
            raise  # pragma: no cover

    return router


__all__ = [
    "create_provider_router",
    "ProviderCreateRequest",
    "ProviderUpdateRequest",
    "RoutingRequest",
]
