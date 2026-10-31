#!/usr/bin/env python3
"""Run the M0 HTTP vertical slice using only Python's standard library."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import re
import subprocess
import sys
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

DEFAULT_BASE_URL = "http://127.0.0.1:7100"
DEFAULT_TOKEN = "lingjingjing-local-dev"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
CJK_RE = re.compile(r"[\u3400-\u9fff]")


class CheckFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str


@dataclass(frozen=True)
class MemoryEvidence:
    query: str
    text: str


class Report:
    def __init__(self) -> None:
        self.results: list[CheckResult] = []

    def add(self, name: str, status: str, detail: str) -> None:
        result = CheckResult(name, status, detail)
        self.results.append(result)
        print(f"[{status}] {name}: {detail}", flush=True)

    def run(self, name: str, action: Callable[[], str]) -> bool:
        try:
            detail = action()
        except (CheckFailure, OSError, ValueError, json.JSONDecodeError) as exc:
            self.add(name, "FAIL", str(exc))
            return False
        self.add(name, "PASS", detail)
        return True

    @property
    def failed(self) -> bool:
        return any(result.status == "FAIL" for result in self.results)

    def print_summary(self) -> None:
        counts = {status: sum(result.status == status for result in self.results) for status in ("PASS", "FAIL", "SKIP")}
        print(f"M0 汇总: PASS={counts['PASS']} FAIL={counts['FAIL']} SKIP={counts['SKIP']}", flush=True)


class ApiClient:
    def __init__(self, base_url: str, token: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> Any:
        url = self.base_url + path
        if query:
            url += "?" + urlencode(query)
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            url,
            data=body,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
                "X-Ling-Token": self.token,
                "User-Agent": "lingjingjing-m0-vertical-slice/1",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                status = response.status
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raw = exc.read(2048)
            preview = raw.decode("utf-8", errors="replace").strip()
            raise CheckFailure(f"{method} {path} 返回 HTTP {exc.code}: {preview[:500]}") from exc
        except URLError as exc:
            raise CheckFailure(f"{method} {path} 连接失败: {exc.reason}") from exc
        except TimeoutError as exc:
            raise CheckFailure(f"{method} {path} 请求超时") from exc
        if not 200 <= status < 300:
            raise CheckFailure(f"{method} {path} 返回 HTTP {status}")
        if len(raw) > MAX_RESPONSE_BYTES:
            raise CheckFailure(f"{method} {path} 响应超过 {MAX_RESPONSE_BYTES} 字节")
        if not raw.strip():
            raise CheckFailure(f"{method} {path} 返回空响应，预期 JSON")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CheckFailure(f"{method} {path} 未返回有效 UTF-8 JSON: {exc}") from exc


def collection(payload: Any, keys: tuple[str, ...]) -> list[Any]:
    """Accept common list envelopes without assuming one response schema."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            if key not in payload:
                continue
            value = payload[key]
            if isinstance(value, list):
                return value
            nested = collection(value, keys)
            if nested or value in ({}, None):
                return nested
        if "data" in payload:
            return collection(payload["data"], keys)
        if any(key in payload for key in ("content", "text", "memory", "event", "type")):
            return [payload]
    raise CheckFailure(f"响应中找不到列表字段 {', '.join(keys)}: {json.dumps(payload, ensure_ascii=False)[:500]}")


def text_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in text_values(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in text_values(item)]
    return []


def chat_text(payload: Any) -> str:
    """Prefer assistant-like fields; never treat an exact request echo as a reply."""
    if isinstance(payload, dict):
        for key in ("reply", "response", "answer", "content", "text", "message"):
            if key in payload:
                values = text_values(payload[key])
                joined = " ".join(part.strip() for part in values if part.strip())
                if joined:
                    return joined
        for key in ("data", "result"):
            if key in payload:
                return chat_text(payload[key])
    values = [part.strip() for part in text_values(payload) if part.strip()]
    return " ".join(values)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def relevant_new_events(
    before: list[Any],
    after: list[Any],
    *,
    marker: str,
    client_id: str,
) -> list[Any]:
    old = {canonical(item) for item in before}
    new_items = [item for item in after if canonical(item) not in old]
    relevant: list[Any] = []
    for item in new_items:
        blob = canonical(item)
        lowered = blob.lower()
        if marker in blob or client_id in blob or any(
            word in lowered for word in ("chat", "message", "conversation", "memory", "聊天", "消息", "记忆")
        ):
            relevant.append(item)
    return relevant


def memory_content(item: Any) -> list[str]:
    """Extract actual memory content from nested objects, not just summaries."""
    if isinstance(item, str):
        return [item] if CJK_RE.search(item) else []
    if isinstance(item, list):
        return [text for child in item for text in memory_content(child)]
    if isinstance(item, dict):
        prioritized: list[str] = []
        for key in ("content", "text", "memory", "value", "summary", "data", "result"):
            if key in item:
                prioritized.extend(memory_content(item[key]))
        if prioritized:
            return prioritized
        return [text for text in text_values(item) if CJK_RE.search(text)]
    return []


def choose_memory(items: list[Any], marker: str) -> MemoryEvidence | None:
    candidates: list[tuple[int, str]] = []
    for item in items:
        for text in memory_content(item):
            cleaned = text.strip()
            if len(cleaned) < 2 or not CJK_RE.search(cleaned):
                continue
            score = (1000 if marker in cleaned else 0) + (100 if "喜欢" in cleaned else 0) + min(len(cleaned), 99)
            candidates.append((score, cleaned))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    # Keep URLs manageable while retaining a meaningful actual-memory query.
    return MemoryEvidence(query="喜欢", text=candidates[0][1][:160])


def memory_matches(items: list[Any], evidence: MemoryEvidence, marker: str) -> bool:
    fragment = evidence.text[:32]
    for item in items:
        blob = canonical(item)
        for text in memory_content(item):
            if evidence.text in text or (len(fragment) >= 4 and fragment in text):
                return True
            if marker in evidence.text and marker in blob:
                return True
    return False


def poll_until(deadline: float, action: Callable[[], Any], interval: float = 0.5) -> Any:
    last_failure: CheckFailure | None = None
    while True:
        try:
            value = action()
            if value:
                return value
        except CheckFailure as exc:
            last_failure = exc
        if time.monotonic() >= deadline:
            if last_failure:
                raise last_failure
            return None
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))


def bounded_output(value: str | None) -> str:
    if not value:
        return ""
    return value.strip()[-1000:]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="M0 竖切：健康、中文聊天、事件、中文记忆和可选重开持久性")
    parser.add_argument("--base-url", default=os.environ.get("LING_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--token", default=os.environ.get("LING_TOKEN", DEFAULT_TOKEN), help="也可用环境变量 LING_TOKEN")
    parser.add_argument("--timeout", type=float, default=10.0, help="单次 HTTP 请求超时秒数")
    parser.add_argument("--settle-timeout", type=float, default=20.0, help="事件/记忆异步落盘等待秒数")
    restart = parser.add_mutually_exclusive_group()
    restart.add_argument("--skip-restart", action="store_true", help="明确跳过重开持久性检查")
    restart.add_argument(
        "--command",
        help="完整重启命令：负责停止并启动服务且返回；返回后轮询健康并验证重启前记忆",
    )
    parser.add_argument("--restart-timeout", type=float, default=120.0, help="重启命令与健康恢复总等待秒数")
    parser.add_argument("--event-limit", type=int, default=100, help="事件查询 limit（默认 100）")
    return parser


def positive(value: float, name: str) -> float:
    if value <= 0:
        raise ValueError(f"{name} 必须大于 0")
    return value


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        positive(args.timeout, "timeout")
        positive(args.settle_timeout, "settle-timeout")
        positive(args.restart_timeout, "restart-timeout")
        if args.event_limit <= 0:
            raise ValueError("event-limit 必须大于 0")
        if not args.token:
            raise ValueError("令牌不能为空")
    except ValueError as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        return 2

    report = Report()
    api = ApiClient(args.base_url, args.token, args.timeout)
    marker = f"m0-{uuid4().hex}"
    client_id = f"m0-client-{uuid4().hex}"
    message = f"你好，请记住：我喜欢喝桂花乌龙，也喜欢雨天散步。本次竖切编号是 {marker}。请用中文回答。"
    print(f"目标: {args.base_url}；本次标记: {marker}", flush=True)

    healthy = report.run(
        "健康检查",
        lambda: f"GET /v1/health => {canonical(api.request('GET', '/v1/health'))[:500]}",
    )
    if not healthy:
        report.add("后续 HTTP 检查", "SKIP", "健康检查失败，为避免误导未继续")
        report.print_summary()
        return 1

    before_events: list[Any] = []
    baseline_ok = report.run(
        "事件基线",
        lambda: _event_baseline(api, args.event_limit, before_events),
    )

    def chat_check() -> str:
        payload = api.request("POST", "/v1/chat", payload={"message": message, "client_id": client_id})
        reply = chat_text(payload)
        if not reply or not CJK_RE.search(reply):
            raise CheckFailure(f"聊天响应缺少中文文本: {canonical(payload)[:800]}")
        if reply.strip() == message.strip():
            raise CheckFailure("聊天响应只是原样回显请求，不能作为中文回复 PASS")
        return f"收到中文回复: {reply[:240]}"

    chat_ok = report.run("中文聊天", chat_check)

    def event_check() -> str:
        if not baseline_ok:
            raise CheckFailure("事件基线失败，无法证明本次聊天产生新事件")
        deadline = time.monotonic() + args.settle_timeout

        def find_events() -> list[Any] | None:
            payload = api.request("GET", "/v1/events", query={"limit": args.event_limit})
            current = collection(payload, ("events", "items", "results"))
            matches = relevant_new_events(before_events, current, marker=marker, client_id=client_id)
            return matches or None

        matches = poll_until(deadline, find_events)
        if not matches:
            raise CheckFailure("等待期内未发现与本次中文聊天相关的新事件")
        return f"发现 {len(matches)} 条本次相关新事件: {canonical(matches[-1])[:500]}"

    events_ok = report.run("事件落盘", event_check) if chat_ok else report.run(
        "事件落盘", lambda: (_ for _ in ()).throw(CheckFailure("中文聊天失败，无法验证事件落盘"))
    )

    evidence: MemoryEvidence | None = None

    def memory_check() -> str:
        nonlocal evidence
        deadline = time.monotonic() + args.settle_timeout

        def find_memory() -> MemoryEvidence | None:
            # “喜欢”是首选；桂花乌龙是本次实际写入的记忆内容，作为健壮回退。
            for query in ("喜欢", "桂花乌龙"):
                payload = api.request("GET", "/v1/memory/search", query={"q": query})
                items = collection(payload, ("memories", "items", "results"))
                selected = choose_memory(items, marker)
                if selected:
                    evidence = MemoryEvidence(query=query, text=selected.text)
                    return evidence
            return None

        selected = poll_until(deadline, find_memory)
        if not selected:
            raise CheckFailure("用“喜欢”及实际记忆内容“桂花乌龙”检索均未得到中文记忆")
        evidence = selected
        return f"q={selected.query!r} 检索到实际中文记忆: {selected.text[:240]}"

    memory_ok = report.run("中文记忆检索", memory_check) if chat_ok else report.run(
        "中文记忆检索", lambda: (_ for _ in ()).throw(CheckFailure("中文聊天失败，无法验证记忆写入与检索"))
    )

    if args.command and chat_ok and memory_ok and evidence is not None:
        def restart_check() -> str:
            assert evidence is not None
            started = time.monotonic()
            try:
                completed = subprocess.run(
                    args.command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=args.restart_timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise CheckFailure(f"重启命令超过 {args.restart_timeout} 秒未完成") from exc
            if completed.returncode != 0:
                detail = (bounded_output(completed.stdout) + "\n" + bounded_output(completed.stderr)).strip()
                raise CheckFailure(f"重启命令退出码 {completed.returncode}: {detail}")

            def health_after_restart() -> bool:
                api.request("GET", "/v1/health")
                return True

            remaining = max(0.1, args.restart_timeout - (time.monotonic() - started))
            if not poll_until(time.monotonic() + remaining, health_after_restart):
                raise CheckFailure("重启后健康检查未恢复")
            api.request("GET", "/v1/state")

            def persisted() -> bool:
                payload = api.request("GET", "/v1/memory/search", query={"q": evidence.text})
                items = collection(payload, ("memories", "items", "results"))
                return memory_matches(items, evidence, marker)

            if not poll_until(time.monotonic() + args.settle_timeout, persisted):
                # A real-content query can be too specific for some indexes;
                # retry the original Chinese query before declaring data loss.
                def persisted_original_query() -> bool:
                    payload = api.request("GET", "/v1/memory/search", query={"q": evidence.query})
                    items = collection(payload, ("memories", "items", "results"))
                    return memory_matches(items, evidence, marker)

                if not poll_until(time.monotonic() + args.settle_timeout, persisted_original_query):
                    raise CheckFailure("重启后未检索到重启前的同一条实际记忆")
            return f"重启命令成功，健康/state 已恢复，重启前记忆仍可检索: {evidence.text[:160]}"

        report.run("重开持久性", restart_check)
    elif args.command:
        report.add("重开持久性", "SKIP", "前置聊天或记忆检查失败，未执行重启命令")
    else:
        reason = "--skip-restart 已指定" if args.skip_restart else "未提供 --command"
        report.add("重开持久性", "SKIP", f"{reason}；没有重开证据，不声明 PASS")

    report.add("显存检查", "SKIP", "纯 HTTP 脚本没有真实 GPU/进程显存证据；请在目标环境用 nvidia-smi 等人工核验")
    report.add("Tauri 检查", "SKIP", "未连接真实 Tauri 桌面窗口；请人工核验启动、中文对话与重开，不伪造 PASS")
    report.print_summary()
    return 1 if report.failed or not (chat_ok and events_ok and memory_ok) else 0


def _event_baseline(api: ApiClient, limit: int, destination: list[Any]) -> str:
    payload = api.request("GET", "/v1/events", query={"limit": limit})
    events = collection(payload, ("events", "items", "results"))
    destination.extend(events)
    return f"记录 {len(events)} 条既有事件用于差分"


if __name__ == "__main__":
    raise SystemExit(main())
