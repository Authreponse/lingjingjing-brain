"""单 GPU 显存互斥队列（队列键 gpu:local:0）。

设计依据 docs/模型清单.md 第 4 节与 docs/熔断规则_v1.json LJ-R011：

- 同一队列键在同一时刻只允许一个任务持有 GPU；运行权由内部锁保证，只有调用
  release() 才归还。
- 状态流转 queued -> loading -> running -> unloading -> succeeded / failed，
  取消进入 cancelled；failed / cancelled 同样要走 unloading 清理。
- 连续 oom_threshold（默认 2）次 OOM 暂停整个队列，写 event 通知主人，保留文本
  降级能力；恢复必须由主人复核后手动 resume()，不自动重试。
- 每次状态变更都通过回调产出，API 层与测试据此观察。

队列是纯内存对象，进程重启即清空：GPU 排队的意义只在同一进程内成立。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol

LOGGER = logging.getLogger("lingbrain.gpu_queue")

DEFAULT_QUEUE_KEY = "gpu:local:0"

# 任务状态常量，供 API / UI 与测试直接引用，避免手写字符串。
QUEUED = "queued"
LOADING = "loading"
RUNNING = "running"
UNLOADING = "unloading"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"

#: 终态集合：进入这些状态后任务不再变化。
TERMINAL_STATES = frozenset({SUCCEEDED, FAILED, CANCELLED})

#: 任务类型 -> 优先级数值，越小越先出队。主人正在进行的对话永远优先。
TASK_PRIORITY: dict[str, int] = {
    "chat": 0,
    "gpu_asr": 5,
    "vlm": 10,
    "image": 20,
}

#: 后台任务等待超过该秒数时，status() 会把它标成 starved，提示主人。
STARVATION_SECONDS = 600.0


class QueuePaused(RuntimeError):
    """队列因连续 OOM 被暂停，新任务不能入队，也不会被调度。"""


class GpuOutOfMemory(RuntimeError):
    """显存不足。生图/推理后端用它表达 OOM，队列据此触发熔断。"""


class GpuUnavailable(RuntimeError):
    """后端不可达、连接失败等可恢复的运行错误（非 OOM）。"""


class MemoryNotReleased(RuntimeError):
    """卸载后显存仍未回落，队列拒绝加载下一个模型（不并行抢卡）。"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _seconds_since(iso_ts: str) -> float:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso_ts)).total_seconds()
    except ValueError:
        return 0.0


@dataclass(slots=True)
class GpuJob:
    task_id: str
    task_type: str
    model_id: str
    timeout_seconds: float = 60.0
    requested_by: str = "system"
    created_at: str = field(default_factory=utc_now)
    payload: dict[str, Any] = field(default_factory=dict)
    state: str = QUEUED
    error: str | None = None
    result: Any = None
    enqueued_seq: int = 0
    enqueued_at: str = field(default_factory=utc_now)
    finished_at: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def priority(self) -> int:
        return TASK_PRIORITY.get(self.task_type, 50)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "model_id": self.model_id,
            "timeout_seconds": self.timeout_seconds,
            "requested_by": self.requested_by,
            "created_at": self.created_at,
            "state": self.state,
            "error": self.error,
            "enqueued_at": self.enqueued_at,
            "finished_at": self.finished_at,
            "history": [dict(item) for item in self.history],
        }


class Notifier(Protocol):
    """通知主人的最小接口。数据库实现见 GpuQueueHook。"""

    def notify(self, message: str, payload: dict[str, Any]) -> None: ...


# 后端与显存探针只依赖回调，不依赖本模块的 Protocol 实现类。
BackendFn = Callable[[GpuJob], Awaitable[Any]]
StatusCallback = Callable[[dict[str, Any]], None]

#: 等待队列变化时的轮询步长（秒）。用 Event + 短睡代替 Condition，逻辑直白、
#: 不会出现超时路径与 waiter 抢锁的问题；单机低频任务这点开销可以忽略。
POLL_INTERVAL = 0.01


class GpuQueue:
    def __init__(
        self,
        queue_key: str = DEFAULT_QUEUE_KEY,
        *,
        oom_threshold: int = 2,
        notifier: Notifier | None = None,
        on_status: StatusCallback | None = None,
    ):
        if oom_threshold < 1:
            raise ValueError("oom_threshold 至少为 1")
        self.queue_key = queue_key
        self.oom_threshold = oom_threshold
        self._notifier = notifier
        self._on_status = on_status
        self._pending: list[GpuJob] = []
        self._running: GpuJob | None = None
        self._seq = 0
        self._consecutive_oom = 0
        self._pause_reason: str | None = None
        self._last_oom_at: str | None = None
        self._completed = 0
        self._failed = 0
        self._cancelled = 0
        self._oom_total = 0

    # ------------------------------------------------------------------ 状态
    @property
    def running(self) -> GpuJob | None:
        return self._running

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def paused(self) -> bool:
        return self._pause_reason is not None

    def status(self) -> dict[str, Any]:
        ordered = sorted(self._pending, key=lambda job: (job.priority, job.enqueued_seq))
        queued = []
        for job in ordered:
            waited = _seconds_since(job.enqueued_at)
            queued.append(
                {
                    "task_id": job.task_id,
                    "task_type": job.task_type,
                    "model_id": job.model_id,
                    "state": job.state,
                    "waited_seconds": round(waited, 3),
                    "starved": waited > STARVATION_SECONDS,
                }
            )
        return {
            "queue_key": self.queue_key,
            "paused": self.paused,
            "pause_reason": self._pause_reason,
            "consecutive_oom": self._consecutive_oom,
            "oom_threshold": self.oom_threshold,
            "oom_total": self._oom_total,
            "last_oom_at": self._last_oom_at,
            "busy": self._running is not None,
            "running": self._running.to_dict() if self._running else None,
            "queued": queued,
            "queued_count": len(queued),
            "completed": self._completed,
            "failed": self._failed,
            "cancelled": self._cancelled,
            "starvation_seconds": STARVATION_SECONDS,
        }

    # -------------------------------------------------------------- 对外接口
    def pause(self, reason: str) -> None:
        """暂停队列并通知主人。暂停后已排队的任务保持 queued，不会被调度。"""
        if self._pause_reason == reason:
            return
        self._pause_reason = reason
        LOGGER.warning("GPU 队列 %s 已暂停：%s", self.queue_key, reason)
        self._notify(f"GPU 队列已暂停：{reason}", {"queue_key": self.queue_key})
        self.status_changed()

    def resume(self) -> None:
        """主人复核后手动恢复。恢复后连续 OOM 计数清零，重新允许调度。"""
        was_paused = self.paused
        self._pause_reason = None
        self._consecutive_oom = 0
        if was_paused:
            LOGGER.warning("GPU 队列 %s 已被主人恢复", self.queue_key)
            self.status_changed()

    async def enqueue(
        self,
        task_type: str,
        model_id: str,
        *,
        timeout_seconds: float = 60.0,
        requested_by: str = "system",
        payload: dict[str, Any] | None = None,
        task_id: str | None = None,
    ) -> GpuJob:
        if self.paused:
            raise QueuePaused(self._pause_reason or "GPU 队列已暂停")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须为正数")
        self._seq += 1
        job = GpuJob(
            task_id=task_id or str(uuid.uuid4()),
            task_type=task_type,
            model_id=model_id,
            timeout_seconds=timeout_seconds,
            requested_by=requested_by,
            payload=payload or {},
            enqueued_seq=self._seq,
        )
        job.history.append({"state": QUEUED, "ts": utc_now()})
        self._pending.append(job)
        self.status_changed(job)
        return job

    async def next_job(self, timeout: float | None = None) -> GpuJob | None:
        """按优先级取下一个任务，并把运行权交给调用方；没有任务时返回 None。"""
        return await self._wait_for(None, timeout)

    async def take(self, job: GpuJob, timeout: float | None = None) -> GpuJob | None:
        """等待并取得指定任务的运行权；超时或条目被撤走时返回 None。"""
        return await self._wait_for(job, timeout)

    async def _wait_for(self, wanted: GpuJob | None, timeout: float | None) -> GpuJob | None:
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while True:
            if self.paused:
                raise QueuePaused(self._pause_reason or "GPU 队列已暂停")
            if wanted is not None and wanted not in self._pending:
                return None
            if self._running is None and self._pending:
                self._pending.sort(key=lambda item: (item.priority, item.enqueued_seq))
                candidate = self._pending[0]
                if wanted is None or candidate is wanted:
                    self._pending.pop(0)
                    self._running = candidate
                    return candidate
            if deadline is not None and loop.time() >= deadline:
                return None
            await asyncio.sleep(POLL_INTERVAL)

    def withdraw(self, job: GpuJob) -> None:
        """把还在排队、未取得运行权的任务撤回成 cancelled。"""
        if job in self._pending:
            self._pending.remove(job)
        self._mark_terminal(job, CANCELLED, "撤回未执行的任务")

    def release(self, job: GpuJob, final_state: str, error: str | None = None) -> None:
        """释放 GPU。只有持有者可以调用；调用后队列才能调度下一个任务。"""
        if self._running is not job:
            raise RuntimeError("当前任务没有持有 GPU，不能释放")
        if final_state not in TERMINAL_STATES:
            raise ValueError(f"非法终态：{final_state}")
        self._running = None
        self._mark_terminal(job, final_state, error)

    def _mark_terminal(self, job: GpuJob, final_state: str, error: str | None) -> None:
        job.state = final_state
        job.error = error
        job.finished_at = utc_now()
        entry: dict[str, Any] = {"state": final_state, "ts": job.finished_at}
        if error:
            entry["error"] = error
        job.history.append(entry)
        if final_state == SUCCEEDED:
            self._completed += 1
        elif final_state == FAILED:
            self._failed += 1
        else:
            self._cancelled += 1
        self.status_changed(job)

    def record_oom(self, job: GpuJob, error: str) -> None:
        """记一次 OOM。连续达到阈值时暂停队列并写通知事件。"""
        self._consecutive_oom += 1
        self._oom_total += 1
        self._last_oom_at = utc_now()
        LOGGER.warning(
            "GPU 任务 %s 发生 OOM（连续第 %s 次）：%s",
            job.task_id,
            self._consecutive_oom,
            error,
        )
        if self._consecutive_oom >= self.oom_threshold:
            self.pause(f"连续 {self._consecutive_oom} 次 OOM，建议降采样后手动复核")

    def record_non_oom_success(self) -> None:
        """一次成功的任务会把连续 OOM 计数清零。"""
        self._consecutive_oom = 0

    # ------------------------------------------------------------- 观测工具
    async def wait_idle(self, timeout: float = 10.0) -> None:
        """测试与验收辅助：等待队列彻底空闲（没有任务也没有持有者）。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if not self._pending and self._running is None:
                return
            if loop.time() > deadline:
                raise TimeoutError("等待 GPU 队列空闲超时")
            await asyncio.sleep(POLL_INTERVAL)

    # --------------------------------------------------------------- 内部
    def _notify(self, message: str, payload: dict[str, Any]) -> None:
        if self._notifier is None:
            return
        try:
            self._notifier.notify(message, payload)
        except Exception:  # 通知失败不能阻断熔断
            LOGGER.exception("GPU 队列通知失败")

    def status_changed(self, job: GpuJob | None = None) -> None:
        if self._on_status is None:
            return
        try:
            self._on_status(
                {
                    "queue_key": self.queue_key,
                    "job": job.to_dict() if job else None,
                    "paused": self.paused,
                    "consecutive_oom": self._consecutive_oom,
                }
            )
        except Exception:
            LOGGER.exception("GPU 队列状态回调失败")


@dataclass(slots=True)
class GpuExecution:
    """一次任务的执行结果；ok=False 时由调用方把 failed 映射成 HTTP 错误。"""

    state: str
    job: GpuJob
    result: Any = None
    error: str | None = None
    oom: bool = False

    @property
    def ok(self) -> bool:
        return self.state == SUCCEEDED


class GpuExecutor:
    """把「队列互斥 + 状态流转 + OOM 熔断 + 显存清理」串成一次前台执行。

    和常驻 worker 相比，这种用法让请求方直接拿到结果，适合单机、低频的生图与
    短推理；后台 worker 若要复用，仍然走 GpuQueue.next_job / release 原语。
    """

    def __init__(
        self,
        queue: GpuQueue,
        *,
        memory_free: Callable[[], Awaitable[bool]] | None = None,
        unload: Callable[[GpuJob], Awaitable[None]] | None = None,
        drain_wait_seconds: float = 2.0,
    ):
        self.queue = queue
        self._memory_free = memory_free
        self._unload_hook = unload
        self.drain_wait_seconds = drain_wait_seconds
        self._unload_ok = True

    async def execute(
        self,
        task_type: str,
        model_id: str,
        load: BackendFn,
        run: BackendFn,
        *,
        timeout_seconds: float = 60.0,
        requested_by: str = "system",
        payload: dict[str, Any] | None = None,
    ) -> GpuExecution:
        job = await self.queue.enqueue(
            task_type,
            model_id,
            timeout_seconds=timeout_seconds,
            requested_by=requested_by,
            payload=payload,
        )
        acquired = await self.queue.take(
            job, timeout=timeout_seconds + self.drain_wait_seconds
        )
        if acquired is None:
            # 队列里还有别的任务在跑：本次排队超时，撤回自己的条目。
            self.queue.withdraw(job)
            return GpuExecution(CANCELLED, job, error="排队超时")
        try:
            return await self._run_holding(job, load, run)
        finally:
            if self.queue.running is job:  # 兜底：任何路径都不能漏释放 GPU
                await self._unload(job)
                self.queue.release(job, FAILED, "内部错误：任务未正常结束")

    async def _run_holding(self, job: GpuJob, load: BackendFn, run: BackendFn) -> GpuExecution:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + job.timeout_seconds
        self._unload_ok = True
        result: Any = None
        error: str | None = None
        oom = False
        final = SUCCEEDED
        try:
            job.state = LOADING
            job.history.append({"state": LOADING, "ts": utc_now()})
            self.queue.status_changed(job)
            await asyncio.wait_for(load(job), timeout=max(0.001, deadline - loop.time()))
            job.state = RUNNING
            job.history.append({"state": RUNNING, "ts": utc_now()})
            self.queue.status_changed(job)
            result = await asyncio.wait_for(
                run(job), timeout=max(0.001, deadline - loop.time())
            )
            job.result = result
            self.queue.record_non_oom_success()
        except asyncio.TimeoutError:
            final, error = FAILED, f"GPU 任务超时（{job.timeout_seconds}s）"
        except GpuOutOfMemory as exc:
            final, error, oom = FAILED, f"显存不足：{exc}", True
            self.queue.record_oom(job, error)
        except Exception as exc:  # 后端异常、连接失败、模型缺失等
            final, error = FAILED, f"{type(exc).__name__}: {exc}"
        finally:
            # 成功、失败、取消都必须卸载清理，且清理在 GPU 仍被占用时完成。
            await self._unload(job)
        job.payload["unload_ok"] = self._unload_ok
        self.queue.release(job, final, error)
        return GpuExecution(final, job, result=result, error=error, oom=oom)

    async def _unload(self, job: GpuJob, timeout_seconds: float = 5.0) -> None:
        job.state = UNLOADING
        job.history.append({"state": UNLOADING, "ts": utc_now()})
        self.queue.status_changed(job)
        self._unload_ok = True
        try:
            if self._unload_hook is not None:
                await asyncio.wait_for(self._unload_hook(job), timeout=timeout_seconds)
            if self._memory_free is not None and not await asyncio.wait_for(
                self._memory_free(), timeout=timeout_seconds
            ):
                raise MemoryNotReleased("卸载后显存仍未释放")
        except Exception:
            # 清理失败只记日志：绝不能因此让 GPU 看起来空闲。
            self._unload_ok = False
            LOGGER.exception("GPU 任务 %s 卸载清理失败", job.task_id)


class GpuQueueHook:
    """把队列状态与熔断通知落进 event 表，供 /v1/events 与桌宠读取。"""

    def __init__(self, db, actor: str = "system"):
        self.db = db
        self.actor = actor

    def attach(self, queue: GpuQueue) -> GpuQueue:
        """把通知与状态回调挂到队列上（外部传入的队列也能记事件）。"""
        queue._notifier = self
        queue._on_status = self.on_status
        return queue

    def notify(self, message: str, payload: dict[str, Any]) -> None:
        self.db.append_event(
            self.actor,
            "gpu.queue.paused",
            {"message": message, **payload},
        )

    def on_status(self, snapshot: dict[str, Any]) -> None:
        job = snapshot.get("job")
        if not job:
            return
        state = job["state"]
        if state not in {LOADING, RUNNING, UNLOADING}:
            return
        self.db.append_event(
            self.actor,
            "gpu.queue.state",
            {
                "queue_key": snapshot["queue_key"],
                "task_id": job["task_id"],
                "task_type": job["task_type"],
                "model_id": job["model_id"],
                "state": state,
            },
        )
