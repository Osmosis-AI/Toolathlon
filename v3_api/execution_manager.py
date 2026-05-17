"""In-memory state manager for v3 task executions.

Owns:
  * ``executions``: every active task execution, keyed by execution_id
  * ``lock_owner_by_key``: per-endpoint locks on ``task:{tid}`` and
    ``conflict:{group_id}``, mapping each lock key to the owning execution_id
  * ``manager_lock``: serialises admission decisions (capacity + lock check
    + reservation) so concurrent ``/start`` requests can't race through a
    stale status check
  * ``infra_lock``: separate lock that serialises shared-infrastructure
    health checks and (re)deploys.  Never held simultaneously with
    ``manager_lock`` so a slow deploy doesn't freeze new admissions on
    other healthy infra.
  * The idle/lifetime reaper task.

This module deliberately does no container work — see ``container_mgr``
for that.  Manager methods return outcome enums; the router maps them to
HTTP responses.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from .models import ToolDef


def log(msg: str) -> None:
    local_time = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    utc_time = datetime.utcnow().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{local_time}][UTC {utc_time}] [v3] {msg}", flush=True)


# ── Conflict groups ──────────────────────────────────────────────────
#
# Tasks listed in the same group share an external account / resource and
# must not run concurrently *on the same endpoint*.  These are endpoint-
# local; if multiple endpoints share a truly global external resource, the
# group needs a distributed lock backend (not implemented in v1 of v3).

TASK_CONFLICT_GROUPS: Dict[str, List[str]] = {
    "calendar_course": [
        "set-conf-cr-ddl",
        "student-interview",
    ],
    "hf_dataset": [
        "huggingface-upload",
        "dataset-license-issue",
    ],
    "woocommerce": [
        "woocommerce-customer-survey",
        "woocommerce-product-recall",
    ],
}

# Reverse index: task_id -> group_id (built once at import).
_TASK_TO_GROUP: Dict[str, str] = {}
for _gid, _tasks in TASK_CONFLICT_GROUPS.items():
    for _t in _tasks:
        _TASK_TO_GROUP[_t] = _gid


def conflict_group_for(task_id: str) -> Optional[str]:
    return _TASK_TO_GROUP.get(task_id)


def lock_keys_for(task_id: str) -> List[str]:
    """Per-execution lock keys.  Always includes ``task:{task_id}``; adds
    ``conflict:{group_id}`` when the task is in a configured conflict group.
    """
    keys = [f"task:{task_id}"]
    group = conflict_group_for(task_id)
    if group is not None:
        keys.append(f"conflict:{group}")
    return keys


# ── Configuration knobs ──────────────────────────────────────────────

MAX_ACTIVE_EXECUTIONS = int(os.environ.get("TOOLATHLON_V3_MAX_ACTIVE_EXECUTIONS", "30"))
TASK_LIFETIME_SECONDS = int(os.environ.get("TOOLATHLON_V3_TASK_LIFETIME_SECONDS", "5400"))    # 90 min after ready
SETUP_TIMEOUT_SECONDS = int(os.environ.get("TOOLATHLON_V3_SETUP_TIMEOUT_SECONDS", "1800"))    # 30 min from /start
IDLE_TIMEOUT_SECONDS = int(os.environ.get("TOOLATHLON_V3_IDLE_TIMEOUT_SECONDS", "1200"))      # 20 min since last activity
REAPER_INTERVAL_SECONDS = int(os.environ.get("TOOLATHLON_V3_REAPER_INTERVAL_SECONDS", "60"))
SKIP_DEPLOY = os.environ.get("TOOLATHLON_V3_SKIP_DEPLOY", "").lower() in ("1", "true", "yes")
INFRA_HEALTH_TTL_SECONDS = float(os.environ.get("TOOLATHLON_V3_INFRA_HEALTH_TTL_SECONDS", "30"))
INFRA_RETRY_AFTER_SECONDS = float(os.environ.get("TOOLATHLON_V3_INFRA_RETRY_AFTER_SECONDS", "30"))


# ── State types ──────────────────────────────────────────────────────

@dataclass
class ExecutionState:
    """One task execution.  Lifecycle:

        starting -> ready -> graded/cleaned
        starting -> (setup fail / cancellation) -> cleaned
        ready    -> (DELETE / watchdog / idle reap / lifetime reap) -> cleaned

    Every cleanup path runs ``manager.cleanup_execution`` exactly once, which
    drops ``manager.executions[execution_id]``, releases every key in
    ``lock_keys``, and stops the container.
    """
    execution_id: str
    task_id: str
    container_name: str
    gateway_port: int
    gateway_url: str
    output_folder: str
    lock_keys: List[str] = field(default_factory=list)
    status: str = "starting"                # starting | ready | grading | stopping
    setup_status: str = "starting"          # starting | ready
    setup_phase: str = "container_start"
    tools: List[ToolDef] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    ready_at: Optional[float] = None
    last_activity_at: float = field(default_factory=time.time)
    setup_task: Optional[asyncio.Task] = None
    watchdog_task: Optional[asyncio.Task] = None
    client_id: Optional[str] = None
    model_name: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def deadline_at(self) -> float:
        """Absolute lifetime backstop.

        Before ready: ``created_at + SETUP_TIMEOUT_SECONDS``.
        After ready:  ``ready_at + TASK_LIFETIME_SECONDS``.
        """
        if self.ready_at is None:
            return self.created_at + SETUP_TIMEOUT_SECONDS
        return self.ready_at + TASK_LIFETIME_SECONDS

    def remaining_lifetime(self) -> float:
        return max(0.0, self.deadline_at() - time.time())


# ── Admission outcomes ───────────────────────────────────────────────

class AdmissionOutcome(str, Enum):
    ADMITTED = "admitted"
    CAPACITY_FULL = "capacity_full"
    TASK_BUSY = "task_busy"
    CONFLICT_GROUP_BUSY = "conflict_group_busy"
    INFRA_CHECKING = "infra_checking"
    INFRA_REPAIRING = "infra_repairing"
    INFRA_FAILED = "infra_failed"
    INFRA_DISABLED_OR_UNKNOWN = "infra_disabled_or_unknown"


@dataclass
class AdmissionResult:
    outcome: AdmissionOutcome
    execution: Optional[ExecutionState] = None
    busy_keys: List[str] = field(default_factory=list)
    retry_after_s: float = 0.0
    deploy_status: Optional[str] = None
    last_infra_error: Optional[str] = None


# ── Manager singleton ────────────────────────────────────────────────

class ExecutionManager:
    """Singleton manager.  Constructed once by the launcher; the router
    delegates all state work to it.
    """

    def __init__(self) -> None:
        self.executions: Dict[str, ExecutionState] = {}
        self.lock_owner_by_key: Dict[str, str] = {}
        self.manager_lock = asyncio.Lock()
        self.infra_lock = asyncio.Lock()
        self.max_active_executions = MAX_ACTIVE_EXECUTIONS
        self.reaper_task: Optional[asyncio.Task] = None
        # deploy status: "unknown" | "checking" | "repairing" | "ready" | "failed" | "disabled_debug"
        self.deploy_status: str = "disabled_debug" if SKIP_DEPLOY else "unknown"
        self.deploy_task: Optional[asyncio.Task] = None
        self.last_infra_check_at: Optional[float] = None
        self.last_infra_error: Optional[str] = None

    # ── Lifecycle ────────────────────────────────────────────────

    def start_reaper(self) -> None:
        if self.reaper_task is None or self.reaper_task.done():
            self.reaper_task = asyncio.create_task(self._reaper_loop())

    async def shutdown(self) -> None:
        """Cancel reaper + all in-flight setup/watchdog tasks and stop every
        active container.  Called by the launcher's shutdown hook so per-task
        containers don't leak when the server is SIGTERM'd.
        """
        if self.reaper_task is not None and not self.reaper_task.done():
            self.reaper_task.cancel()
            try:
                await self.reaper_task
            except asyncio.CancelledError:
                pass
            self.reaper_task = None

        # Cancel inflight tasks first; cleanup_execution below will docker rm.
        for ex in list(self.executions.values()):
            for t in (ex.setup_task, ex.watchdog_task):
                if t is not None and not t.done():
                    t.cancel()

        for ex in list(self.executions.values()):
            try:
                await self.cleanup_execution(ex, reason="shutdown")
            except Exception as e:
                log(f"shutdown: cleanup_execution({ex.execution_id}) failed: {e!r}")

    # ── Cached state queries ─────────────────────────────────────

    def cached_infra_is_fresh(self) -> bool:
        if self.deploy_status != "ready" or self.last_infra_check_at is None:
            return False
        return (time.time() - self.last_infra_check_at) < INFRA_HEALTH_TTL_SECONDS

    def busy_locks_snapshot(self) -> Dict[str, str]:
        return dict(self.lock_owner_by_key)

    def active_count(self) -> int:
        return len(self.executions)

    def capacity_available(self) -> bool:
        return self.active_count() < self.max_active_executions

    # ── Admission ────────────────────────────────────────────────

    async def try_admit(
        self,
        task_id: str,
        container_name: str,
        gateway_port: int,
        gateway_url: str,
        output_folder: str,
        *,
        model_name: Optional[str],
        client_id: Optional[str],
        metadata: Dict[str, Any],
    ) -> AdmissionResult:
        """Atomically check capacity + locks and, if all green, register
        a fresh ExecutionState.  The caller is responsible for spawning the
        setup task on the returned execution.

        This does NOT call ``ensure_shared_infra_ready`` — the router runs
        that first (without holding ``manager_lock``) so a slow shared-infra
        repair never blocks unrelated admissions.
        """
        keys = lock_keys_for(task_id)
        async with self.manager_lock:
            # Re-verify deploy status under the lock.  ensure_shared_infra_ready
            # ran without manager_lock, so it could have transitioned by now.
            if self.deploy_status not in ("ready", "disabled_debug"):
                outcome = {
                    "checking":  AdmissionOutcome.INFRA_CHECKING,
                    "repairing": AdmissionOutcome.INFRA_REPAIRING,
                    "failed":    AdmissionOutcome.INFRA_FAILED,
                    "unknown":   AdmissionOutcome.INFRA_DISABLED_OR_UNKNOWN,
                }.get(self.deploy_status, AdmissionOutcome.INFRA_DISABLED_OR_UNKNOWN)
                return AdmissionResult(
                    outcome=outcome,
                    retry_after_s=INFRA_RETRY_AFTER_SECONDS,
                    deploy_status=self.deploy_status,
                    last_infra_error=self.last_infra_error,
                )

            if not self.capacity_available():
                return AdmissionResult(
                    outcome=AdmissionOutcome.CAPACITY_FULL,
                    retry_after_s=10.0,
                )

            busy_keys = [k for k in keys if k in self.lock_owner_by_key]
            if busy_keys:
                task_busy = any(k.startswith("task:") for k in busy_keys)
                outcome = AdmissionOutcome.TASK_BUSY if task_busy else AdmissionOutcome.CONFLICT_GROUP_BUSY
                # Pick a sensible retry-after based on the youngest competing
                # execution's remaining lifetime.  Cheap heuristic; clients
                # will jitter on top.
                competitor_age = min(
                    (
                        time.time() - self.executions[self.lock_owner_by_key[k]].created_at
                        for k in busy_keys
                        if self.lock_owner_by_key.get(k) in self.executions
                    ),
                    default=10.0,
                )
                return AdmissionResult(
                    outcome=outcome,
                    busy_keys=busy_keys,
                    retry_after_s=max(5.0, 30.0 - competitor_age),
                )

            execution_id = f"exec_{uuid.uuid4().hex[:8]}"
            now = time.time()
            ex = ExecutionState(
                execution_id=execution_id,
                task_id=task_id,
                container_name=container_name,
                gateway_port=gateway_port,
                gateway_url=gateway_url,
                output_folder=output_folder,
                lock_keys=list(keys),
                created_at=now,
                last_activity_at=now,
                client_id=client_id,
                model_name=model_name,
                metadata=dict(metadata),
            )
            self.executions[execution_id] = ex
            for k in keys:
                self.lock_owner_by_key[k] = execution_id

            log(
                f"Admitted {execution_id} task={task_id} locks={keys} "
                f"active={self.active_count()}/{self.max_active_executions}"
            )
            return AdmissionResult(outcome=AdmissionOutcome.ADMITTED, execution=ex)

    # ── Cleanup ──────────────────────────────────────────────────

    async def cleanup_execution(self, execution: ExecutionState, *, reason: str) -> None:
        """Single converging cleanup path used by every terminal outcome.

        Does NOT cancel ``setup_task`` or ``watchdog_task`` — callers are
        expected to handle cancellation themselves (or, for the
        watchdog-driven path, the watchdog task is the caller and is about
        to exit anyway).  This keeps the lock window short.
        """
        async with self.manager_lock:
            existed = self.executions.pop(execution.execution_id, None) is not None
            if existed:
                for k in execution.lock_keys:
                    if self.lock_owner_by_key.get(k) == execution.execution_id:
                        self.lock_owner_by_key.pop(k, None)
                log(
                    f"Cleaned {execution.execution_id} task={execution.task_id} "
                    f"reason={reason} released_locks={execution.lock_keys} "
                    f"active={self.active_count()}/{self.max_active_executions}"
                )

        # Container teardown happens outside manager_lock — docker rm is slow.
        # Import lazily to avoid a circular dep.
        from .container_mgr import teardown_container
        await teardown_container(execution)

    # ── Activity / status helpers ────────────────────────────────

    def touch_activity(self, execution: ExecutionState) -> None:
        execution.last_activity_at = time.time()

    def get(self, execution_id: str) -> Optional[ExecutionState]:
        return self.executions.get(execution_id)

    # ── Shared-infra readiness helper ─────────────────────────────

    async def ensure_shared_infra_ready(self, *, trigger_repair: bool) -> bool:
        """Authoritative readiness gate for ``/start`` (and observability for
        ``/health``).  Returns True if shared infra is healthy now; False if
        we cannot admit a task right now.

        Concurrency:
          * If cached health is fresh and ``deploy_status == "ready"``, this
            returns True immediately, no lock taken.
          * Otherwise, only one caller is allowed inside ``infra_lock`` at a
            time.  Other callers see ``infra_lock.locked()`` and return False
            without spawning a parallel deploy.
        """
        if self.deploy_status == "disabled_debug":
            return True
        if self.cached_infra_is_fresh():
            return True

        if self.infra_lock.locked():
            return False

        async with self.infra_lock:
            # Re-check after acquiring the lock — a peer may have finished.
            if self.deploy_status == "disabled_debug":
                return True
            if self.cached_infra_is_fresh():
                return True

            from .container_mgr import (
                fast_shared_infra_health_check,
                run_shared_infra_deploy,
            )

            self.deploy_status = "checking"
            healthy, err = await fast_shared_infra_health_check()
            if healthy:
                self.deploy_status = "ready"
                self.last_infra_error = None
                self.last_infra_check_at = time.time()
                return True

            if not trigger_repair:
                self.deploy_status = "failed"
                self.last_infra_error = err or "fast health probe failed"
                return False

            self.deploy_status = "repairing"
            try:
                await run_shared_infra_deploy()
                ok_after, err_after = await fast_shared_infra_health_check()
                if not ok_after:
                    raise RuntimeError(
                        f"shared infra still unhealthy after redeploy: {err_after}"
                    )
                self.deploy_status = "ready"
                self.last_infra_error = None
                self.last_infra_check_at = time.time()
                return True
            except Exception as exc:
                self.deploy_status = "failed"
                self.last_infra_error = repr(exc)
                log(f"shared-infra deploy FAILED: {exc!r}")
                return False

    # ── Reaper loop ──────────────────────────────────────────────

    async def _reaper_loop(self) -> None:
        """Background task that enforces:
          * setup_timeout (created_at -> ready), per execution
          * idle_timeout (last_activity_at), per execution
          * task lifetime (ready_at + TASK_LIFETIME_SECONDS), per execution
        """
        try:
            while True:
                await asyncio.sleep(REAPER_INTERVAL_SECONDS)
                now = time.time()
                victims: List[tuple[ExecutionState, str]] = []

                for ex in list(self.executions.values()):
                    if ex.setup_status != "ready":
                        if now - ex.created_at > SETUP_TIMEOUT_SECONDS:
                            victims.append((ex, f"setup exceeded {SETUP_TIMEOUT_SECONDS}s"))
                            continue
                    else:
                        if ex.ready_at is not None and (now - ex.ready_at) > TASK_LIFETIME_SECONDS:
                            victims.append((ex, f"lifetime exceeded {TASK_LIFETIME_SECONDS}s"))
                            continue
                    if (now - ex.last_activity_at) > IDLE_TIMEOUT_SECONDS:
                        victims.append((ex, f"idle for {(now - ex.last_activity_at) / 60:.1f} min"))

                for ex, reason in victims:
                    log(f"reaper: {ex.execution_id} ({reason}), auto-cleaning")
                    for t in (ex.setup_task, ex.watchdog_task):
                        if t is not None and not t.done():
                            t.cancel()
                    try:
                        await self.cleanup_execution(ex, reason=f"reaper:{reason}")
                    except Exception as e:
                        log(f"reaper: cleanup_execution({ex.execution_id}) failed: {e!r}")
        except asyncio.CancelledError:
            pass


# Module-level singleton.  The launcher constructs it once; the router
# imports this name.
manager = ExecutionManager()
