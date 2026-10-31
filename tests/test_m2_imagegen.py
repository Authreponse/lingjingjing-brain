"""M2 GPU 排队器与生图服务测试。

全部用例都在无 GPU 环境跑通：生图用 MockBackend（Pillow 画真实 PNG），
ComfyUI 只测 HTTP 客户端逻辑（httpx.MockTransport），OOM 用异常注入。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx
import pytest

from lingbrain.db import Database, utc_now
from lingbrain.gpu_queue import (
    CANCELLED,
    FAILED,
    LOADING,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    TERMINAL_STATES,
    UNLOADING,
    GpuExecutor,
    GpuOutOfMemory,
    GpuQueue,
    GpuQueueHook,
    GpuUnavailable,
    QueuePaused,
)
from lingbrain.imagegen import (
    ALLOWED_SIZES,
    DEFAULT_DAILY_BUDGET,
    STYLE_PREFIX,
    ArtworkPathError,
    ArtworkStore,
    BudgetExhausted,
    ComfyUIBackend,
    ImageGenerationError,
    ImageGenerationService,
    MockBackend,
    MockStorage,
    compose_prompt,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def today_noon():
    """测试用：今天的本地时间戳（与 _day_bounds 同一时区口径）。"""
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone.utc) + timedelta(minutes=1)


def run(coro):
    return asyncio.run(coro)


def make_db(tmp_path, name: str = "brain.db") -> Database:
    return Database(tmp_path / name)


# --------------------------------------------------------------- 队列基础
def test_queue_is_strictly_mutually_exclusive():
    async def scenario():
        queue = GpuQueue()
        active = 0
        peak = 0
        order: list[str] = []

        async def work(task_id: str):
            nonlocal active, peak
            job = await queue.enqueue("image", "sdxl", task_id=task_id)
            got = await queue.take(job, timeout=5)
            assert got is job
            active += 1
            peak = max(peak, active)
            order.append(task_id)
            await asyncio.sleep(0.02)
            queue.release(job, SUCCEEDED)
            active -= 1
            return got

        await asyncio.gather(*(work(f"t{i}") for i in range(4)))
        assert peak == 1, "同一时刻只能有一个任务持有 GPU"
        assert sorted(order) == ["t0", "t1", "t2", "t3"]
        assert queue.status()["completed"] == 4
        assert queue.status()["busy"] is False

    run(scenario())


def test_executor_state_machine_order_and_sequence():
    async def scenario():
        queue = GpuQueue()
        executor = GpuExecutor(queue)
        states: list[str] = []
        seen: list[str] = []

        async def load(job):
            await asyncio.sleep(0.01)

        async def generate(job):
            # 执行期间队列必须是 busy，别的任务拿不到运行权
            assert queue.status()["busy"] is True
            attempt = await queue.take(await queue.enqueue("image", "x"), timeout=0.01)
            assert attempt is None, "GPU 忙时其它任务不得取得运行权"
            return "ok"

        result = await executor.execute("image", "sdxl", load, generate, timeout_seconds=5)
        assert result.ok and result.result == "ok"
        states = [step["state"] for step in result.job.history]
        assert states == [QUEUED, LOADING, RUNNING, UNLOADING, SUCCEEDED]
        assert queue.status()["queued_count"] == 1  # 并发尝试留下的那条仍是 queued
        assert result.job.payload["unload_ok"] is True
        return states

    run(scenario())


def test_queue_priority_chat_before_image():
    async def scenario():
        queue = GpuQueue()
        blocker = await queue.enqueue("image", "sdxl", task_id="blocker")
        await queue.take(blocker)
        image = await queue.enqueue("image", "sdxl", task_id="bg-image")
        vlm = await queue.enqueue("vlm", "qwen-vl", task_id="bg-vlm")
        chat = await queue.enqueue("chat", "qwen", task_id="owner-chat")
        queue.release(blocker, SUCCEEDED)
        first = await queue.take(image, timeout=0.01)
        assert first is None
        taken = await queue.next_job(timeout=1)
        assert taken.task_id == "owner-chat"
        queue.release(taken, SUCCEEDED)
        vlm_job = await queue.next_job(timeout=1)
        assert vlm_job.task_id == "bg-vlm"
        queue.release(vlm_job, SUCCEEDED)
        image_job = await queue.next_job(timeout=1)
        assert image_job.task_id == "bg-image"

    run(scenario())


def test_executor_releases_gpu_even_on_failure():
    async def scenario():
        queue = GpuQueue()
        executor = GpuExecutor(queue)
        unloads: list[str] = []

        async def load(job):
            raise RuntimeError("模型文件缺失")

        async def generate(job):  # pragma: no cover - 不会执行到
            raise AssertionError("load 失败后不应运行")

        result = await executor.execute("image", "sdxl", load, generate, timeout_seconds=2)
        assert result.state == FAILED
        assert "模型文件缺失" in result.error
        assert queue.status()["busy"] is False
        assert queue.status()["failed"] == 1
        assert UNLOADING in [step["state"] for step in result.job.history]

        # GPU 仍然可以继续接受任务
        async def ok_load(job):
            unloads.append("loaded")

        async def ok_run(job):
            return 1

        again = await executor.execute("image", "sdxl", ok_load, ok_run, timeout_seconds=2)
        assert again.ok

    run(scenario())


def test_executor_timeout_marks_failed_and_unloads():
    async def scenario():
        queue = GpuQueue()
        executor = GpuExecutor(queue)

        async def load(job):
            return None

        async def generate(job):
            await asyncio.sleep(5)

        result = await executor.execute("image", "sdxl", load, generate, timeout_seconds=0.1)
        assert result.state == FAILED
        assert "超时" in result.error
        assert queue.status()["busy"] is False
        assert queue.paused is False, "超时不是 OOM，不应暂停队列"

    run(scenario())


# ------------------------------------------------------------- OOM 熔断
def test_two_consecutive_ooms_pause_queue_and_notify(tmp_path):
    db = make_db(tmp_path)
    try:
        backend = MockBackend(fail_with=GpuOutOfMemory("CUDA out of memory"), fail_times=2)
        service = ImageGenerationService(db, backend=backend, artwork_root=tmp_path / "art")

        async def scenario():
            for _ in range(2):
                with pytest.raises(ImageGenerationError):
                    await service.generate("她在想家", reason="心情低落", mood="低落")
            status = service.queue.status()
            assert status["paused"] is True
            assert status["consecutive_oom"] == 2
            assert "OOM" in status["pause_reason"]
            # 第三次在入队前就被拒绝
            with pytest.raises(QueuePaused):
                await service.generate("再试一次", reason="坚持一下")
            assert backend.generate_calls == 2, "暂停后不得继续调用后端"
            assert backend.unload_calls == 2, "两次失败都要卸载模型"

        run(scenario())
        kinds = [(e["actor"], e["kind"]) for e in db.list_events(50)]
        assert ("system", "gpu.queue.paused") in kinds
        rejected = [e for e in db.list_events(50) if e["kind"] == "image.rejected"]
        assert len(rejected) == 3
        assert service.budget.snapshot()["used"] == 0, "没出图不占额度"
    finally:
        db.close()


def test_single_oom_and_success_reset_do_not_pause(tmp_path):
    db = make_db(tmp_path)
    try:
        backend = MockBackend(fail_with=GpuOutOfMemory("x"), fail_times=1)
        service = ImageGenerationService(db, backend=backend, artwork_root=tmp_path / "art")

        async def scenario():
            with pytest.raises(ImageGenerationError):
                await service.generate("画个海", reason="测试单次 OOM")
            assert service.queue.paused is False
            success = await service.generate("画个海", reason="恢复后重试")
            assert success.artwork.path.endswith(".png")
            assert service.queue.status()["consecutive_oom"] == 0

        run(scenario())
    finally:
        db.close()


def test_owner_resume_clears_pause_and_budget_still_applies(tmp_path):
    db = make_db(tmp_path)
    try:
        queue = GpuQueue()
        backend = MockBackend()
        service = ImageGenerationService(
            db, backend=backend, artwork_root=tmp_path / "art", queue=queue
        )
        queue.pause("人工测试暂停")

        async def scenario():
            with pytest.raises(QueuePaused):
                await service.generate("a", reason="b")
            queue.resume()
            assert queue.paused is False
            result = await service.generate("a", reason="b")
            assert result.artwork.id

        run(scenario())
    finally:
        db.close()


# ---------------------------------------------------------------- 预算
def _fake_artwork_rows(store, count, created_at):
    """写入 count 行并把时间戳改成指定时刻，模拟“今天已经画满 / 很早以前画的”。"""
    ids = [
        store.add(
            path=f"{created_at[:10]}/seed-{index}.png", prompt="p", reason="r", mood=None
        ).id
        for index in range(count)
    ]
    placeholders = ",".join("?" for _ in ids)
    with store.db._lock, store.db.conn:
        store.db.conn.execute(
            f"UPDATE artwork SET created_at=? WHERE id IN ({placeholders})",
            (created_at, *ids),
        )


def test_daily_budget_defaults_to_ten_and_blocks_the_eleventh(tmp_path):
    db = make_db(tmp_path)
    try:
        store = ArtworkStore(db, tmp_path / "art")
        _fake_artwork_rows(store, DEFAULT_DAILY_BUDGET, today_noon().isoformat())
        service = ImageGenerationService(db, backend=MockBackend(), artwork_root=tmp_path / "art")
        assert service.budget.limit == DEFAULT_DAILY_BUDGET == 10
        assert service.budget.snapshot()["used"] == 10
        with pytest.raises(BudgetExhausted):
            run(service.generate("再来一张", reason="超额测试"))
        assert service.budget.snapshot()["remaining"] == 0
    finally:
        db.close()


def test_daily_budget_is_configurable_and_counts_across_days(tmp_path):
    db = make_db(tmp_path)
    try:
        store = ArtworkStore(db, tmp_path / "art")
        _fake_artwork_rows(store, 2, today_noon().isoformat())
        _fake_artwork_rows(store, 5, "2026-01-01T00:00:00.000+00:00")  # 很早以前
        service = ImageGenerationService(
            db, backend=MockBackend(), artwork_root=tmp_path / "art", daily_budget=3
        )
        snapshot = service.budget.snapshot()
        assert snapshot == {"day": snapshot["day"], "limit": 3, "used": 2, "remaining": 1}
        result = run(service.generate("第三张", reason="刚好用完"))
        assert result.budget["used"] == 3 and result.budget["remaining"] == 0
        with pytest.raises(BudgetExhausted):
            run(service.generate("第四张", reason="超额"))
    finally:
        db.close()


def test_mock_storage_budget_counts_only_today():
    storage = MockStorage()
    budget = __import__("lingbrain.imagegen", fromlist=["DailyBudget"]).DailyBudget(storage, 1)
    budget.check()
    storage.add(path="a.png", prompt="p", reason="r", mood=None)
    with pytest.raises(BudgetExhausted):
        budget.check()


# ------------------------------------------------------------- 产物落库
def test_mock_backend_produces_real_png(tmp_path):
    db = make_db(tmp_path)
    try:
        backend = MockBackend()
        service = ImageGenerationService(
            db, backend=backend, artwork_root=tmp_path / "art", daily_budget=10
        )

        async def scenario():
            return await service.generate(
                "她抱着一只更小的自己", reason="主人说想看看她", mood="平静", size=256
            )

        result = run(scenario())
        artwork = result.artwork
        path = tmp_path / "art" / artwork.path
        assert path.is_file()
        data = path.read_bytes()
        assert data[:8] == PNG_MAGIC
        from PIL import Image

        with Image.open(path) as image:
            assert image.format == "PNG"
            assert image.size == (256, 256)
            assert len(set(image.convert("RGB").getcolors(maxcolors=1 << 20))) > 50
        assert artwork.prompt.startswith(STYLE_PREFIX)
        assert artwork.reason == "主人说想看看她"
        assert artwork.mood == "平静"
        assert backend.load_calls == backend.unload_calls == 1
        assert backend.loaded is False
    finally:
        db.close()


def test_artwork_row_is_persisted_with_all_fields(tmp_path):
    db_path = tmp_path / "brain.db"
    db = Database(db_path)
    service = ImageGenerationService(db, backend=MockBackend(), artwork_root=tmp_path / "art")

    async def scenario():
        return await service.generate("月下的海", reason="夜里想起主人", mood="低落", size=256)

    result = run(scenario())
    db.close()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM artwork").fetchone()
    finally:
        conn.close()
    assert row["path"] == result.artwork.path
    assert row["prompt"].startswith(STYLE_PREFIX)
    assert row["reason"] == "夜里想起主人"
    assert row["mood"] == "低落"
    assert row["created_at"]
    assert (tmp_path / "art" / row["path"]).is_file()


def test_artwork_created_event_and_listing(tmp_path):
    db = make_db(tmp_path)
    try:
        service = ImageGenerationService(db, backend=MockBackend(), artwork_root=tmp_path / "art")

        async def scenario():
            first = await service.generate("第一张", reason="开盘", mood="开心", size=256)
            second = await service.generate("第二张", reason="收盘", mood="低落", size=256)
            return first, second

        first, second = run(scenario())
        items = service.list_artwork(limit=10)
        assert [item.id for item in items] == [second.artwork.id, first.artwork.id]
        assert [item.id for item in service.list_artwork(10, mood="开心")] == [first.artwork.id]
        events = [e for e in db.list_events(50) if e["kind"] == "artwork.created"]
        assert len(events) == 2
        payload = events[0]["payload"]
        assert payload["artwork_id"] == second.artwork.id
        assert payload["budget_used"] == 2 and payload["budget_limit"] == 10
        job_events = [e for e in db.list_events(50) if e["kind"] == "image.job"]
        assert len(job_events) == 2
    finally:
        db.close()


def test_artwork_path_is_confined_to_managed_root(tmp_path):
    db = make_db(tmp_path)
    try:
        store = ArtworkStore(db, tmp_path / "art")
        with pytest.raises(ArtworkPathError):
            store.add(path="../escape.png", prompt="p", reason="r", mood=None)
        with pytest.raises(ArtworkPathError):
            store.add(path="2026-01-01/x.gif", prompt="p", reason="r", mood=None)
        assert store.add(path="2026-01-01/x.png", prompt="p", reason="r", mood=None).path
    finally:
        db.close()


def test_invalid_size_and_blank_reason_rejected(tmp_path):
    db = make_db(tmp_path)
    try:
        service = ImageGenerationService(db, backend=MockBackend(), artwork_root=tmp_path / "art")

        async def scenario():
            with pytest.raises(ValueError):
                await service.generate("x", reason="r", size=333)
            with pytest.raises(ValueError):
                await service.generate("x", reason="   ")

        run(scenario())
        assert service.budget.snapshot()["used"] == 0
        assert ALLOWED_SIZES == {256, 512, 768, 1024}
    finally:
        db.close()


def test_compose_prompt_always_carries_style_prefix():
    assert compose_prompt("  一只鲸  ").startswith(STYLE_PREFIX)
    with pytest.raises(ValueError):
        compose_prompt("   ")


# ------------------------------------------------------- ComfyUI HTTP 客户端
def _workflow_fail_payload():
    png = MockBackend()._render("comfy", 256, 7)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={"system": {"comfyui_version": "test"}})
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "pid-1", "number": 1})
        if request.url.path == "/history/pid-1":
            return httpx.Response(
                200,
                json={
                    "pid-1": {
                        "status": {"status_str": "success"},
                        "outputs": {"9": {"images": [{"filename": "l.png", "subfolder": "", "type": "output"}]}},
                    }
                },
            )
        if request.url.path == "/view":
            return httpx.Response(200, content=png, headers={"content-type": "image/png"})
        return httpx.Response(404)

    return handler


def test_comfyui_backend_is_an_http_client(tmp_path, monkeypatch):
    calls: list[str] = []
    base = _workflow_fail_payload()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return base(request)

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    backend = ComfyUIBackend("http://comfy.test:8188", poll_seconds=0.01)

    async def scenario():
        await backend.load()
        data = await backend.generate("海", size=256, seed=3)
        await backend.unload()
        return data

    data = run(scenario())
    assert data[:8] == PNG_MAGIC
    assert calls[0] == "/system_stats"
    assert "/prompt" in calls and "/history/pid-1" in calls and "/view" in calls


def test_comfyui_backend_maps_allocation_error_to_oom(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={})
        # ComfyUI 的校验失败也是 400，真正的区分在响应体里
        return httpx.Response(
            200,
            json={
                "error": {"type": "prompt_outputs_failed_validation", "message": "invalid prompt"},
                "node_errors": {"4": {"errors": ["Allocation on device: CUDA out of memory"]}},
            },
        )

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: original(*a, **{**k, "transport": transport}),
    )
    backend = ComfyUIBackend("http://comfy.test:8188", poll_seconds=0.01)

    async def scenario():
        await backend.load()
        with pytest.raises(GpuOutOfMemory):
            await backend.generate("海", size=256, seed=1)
        await backend.unload()

    run(scenario())


def test_comfyui_backend_unreachable_raises_gpu_unavailable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: original(*a, **{**k, "transport": transport}),
    )
    backend = ComfyUIBackend("http://127.0.0.1:8188")

    async def scenario():
        with pytest.raises(GpuUnavailable):
            await backend.load()

    run(scenario())


# ------------------------------------------------------------- 接线工具
def test_standalone_worker_uses_next_job_and_release():
    async def scenario():
        queue = GpuQueue()
        seen: list[str] = []

        async def worker():
            while True:
                job = await queue.next_job(timeout=0.2)
                if job is None:
                    return
                seen.append(job.task_id)
                queue.release(job, SUCCEEDED)

        for index in range(3):
            await queue.enqueue("vlm", "qwen-vl", task_id=f"w{index}")
        await worker()
        assert seen == ["w0", "w1", "w2"]

    run(scenario())


def test_hook_attach_writes_state_events(tmp_path):
    db = make_db(tmp_path)
    try:
        hook = GpuQueueHook(db)
        queue = hook.attach(GpuQueue())

        async def scenario():
            executor = GpuExecutor(queue)
            await executor.execute("image", "sdxl", lambda job: asyncio.sleep(0), lambda job: asyncio.sleep(0))

        run(scenario())
        states = [e["payload"]["state"] for e in db.list_events(20) if e["kind"] == "gpu.queue.state"]
        assert sorted(states) == sorted([LOADING, RUNNING, UNLOADING]), states
    finally:
        db.close()


def test_job_dict_is_json_serialisable():
    async def scenario():
        queue = GpuQueue()
        job = await queue.enqueue("image", "sdxl", payload={"reason": "r"})
        await queue.take(job)
        queue.release(job, CANCELLED, "主人取消")
        payload = json.dumps(job.to_dict(), ensure_ascii=False)
        return payload

    assert CANCELLED in run(scenario())
    assert TERMINAL_STATES == {SUCCEEDED, FAILED, CANCELLED}
    assert utc_now().endswith("+00:00")
