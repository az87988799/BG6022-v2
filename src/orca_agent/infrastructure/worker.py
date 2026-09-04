"""Fail-closed, one-shot outbox worker with fenced dispatch permits."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from orca_agent.application.effect_completion import EffectCompletionService
from orca_agent.application.errors import (
    EffectDispatchBlockedError,
    LeaseLostError,
    StorageError,
)
from orca_agent.domain.ids import EffectId, WorkerId, new_id
from orca_agent.orchestration.codes import HandlerErrorCode
from orca_agent.orchestration.dispatch_policy import (
    DEFAULT_EFFECT_REGISTRY,
    EffectRegistry,
)
from orca_agent.orchestration.effect_receipts import (
    EffectSuccessReceiptV1,
    parse_effect_success_receipt,
)

from .clock import Clock, SystemClock
from .outbox import DispatchPermit
from .sqlite import resolve_database_path
from .unit_of_work import SQLiteUnitOfWork


@dataclass(frozen=True)
class HandlerResult:
    """Closed worker-to-kernel completion input."""

    success: bool
    error_code: HandlerErrorCode | None = None
    error_message: str | None = None
    result_summary: EffectSuccessReceiptV1 | object | None = None


@dataclass(frozen=True)
class DeliveryReport:
    effect_id: EffectId
    outcome: str
    attempt_count: int


Handler = Callable[[DispatchPermit], HandlerResult]


class OutboxWorker:
    """Claim, authorize, handle, and atomically complete one effect at a time."""

    def __init__(
        self,
        database_path: str | Path | None = None,
        handler: Handler | None = None,
        *,
        state_root: str | Path | None = None,
        clock: Clock | None = None,
        worker_id: WorkerId | None = None,
        lease_duration: timedelta = timedelta(seconds=30),
        max_attempts: int = 5,
        registry: EffectRegistry = DEFAULT_EFFECT_REGISTRY,
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if database_path is None and state_root is None:
            raise ValueError("database_path or state_root is required")
        if database_path is not None and state_root is not None:
            raise ValueError("database_path and state_root are mutually exclusive")
        if handler is None:
            raise ValueError("handler is required")
        configured_path = database_path if database_path is not None else state_root
        self.database_path = resolve_database_path(configured_path)  # type: ignore[arg-type]
        self.handler = handler
        self.clock = clock or SystemClock()
        self.worker_id = worker_id or new_id(WorkerId)
        self.lease_duration = lease_duration
        self.max_attempts = max_attempts
        self.registry = registry

    def run_once(self, *, limit: int = 1) -> tuple[DeliveryReport, ...]:
        """Deliver at most ``limit`` effects without invoking a handler pre-authorization."""

        if type(limit) is not int or limit < 1:
            return ()
        reports: list[DeliveryReport] = []
        for _ in range(limit):
            now = self.clock.now_utc()
            with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
                if (
                    uow.runs is None
                    or uow.events is None
                    or uow.interrupts is None
                    or uow.outbox is None
                ):
                    raise StorageError("kernel repositories are unavailable")
                claimed = uow.outbox.claim_due_verified(
                    runs=uow.runs,
                    events=uow.events,
                    interrupts=uow.interrupts,
                    worker_id=self.worker_id,
                    now=now,
                    lease_duration=self.lease_duration,
                    limit=1,
                    registry=self.registry,
                )
            if not claimed:
                break
            claimed_effect = claimed[0]
            try:
                with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
                    if (
                        uow.runs is None
                        or uow.events is None
                        or uow.interrupts is None
                        or uow.outbox is None
                    ):
                        raise StorageError("kernel repositories are unavailable")
                    permit = uow.outbox.authorize_dispatch(
                        runs=uow.runs,
                        events=uow.events,
                        interrupts=uow.interrupts,
                        effect_id=claimed_effect.effect_id,
                        worker_id=self.worker_id,
                        expected_generation=claimed_effect.attempt_count,
                        now=self.clock.now_utc(),
                        registry=self.registry,
                    )
            except LeaseLostError:
                reports.append(
                    DeliveryReport(
                        claimed_effect.effect_id,
                        "lease_lost",
                        claimed_effect.attempt_count,
                    )
                )
                continue
            except EffectDispatchBlockedError:
                reports.append(
                    DeliveryReport(
                        claimed_effect.effect_id,
                        "blocked",
                        claimed_effect.attempt_count,
                    )
                )
                continue
            if permit is None:
                reports.append(
                    DeliveryReport(
                        claimed_effect.effect_id,
                        "cancelled",
                        claimed_effect.attempt_count,
                    )
                )
                continue

            try:
                normalized = _normalize_handler_result(self.handler(permit))
            except Exception:
                normalized = HandlerResult(
                    success=False,
                    error_code=HandlerErrorCode.HANDLER_EXCEPTION,
                )
            try:
                completion = EffectCompletionService(
                    self.database_path,
                    clock=self.clock,
                    registry=self.registry,
                    max_attempts=self.max_attempts,
                ).complete(permit, normalized)
            except LeaseLostError:
                reports.append(
                    DeliveryReport(permit.effect.effect_id, "lease_lost", permit.generation)
                )
                continue
            reports.append(
                DeliveryReport(
                    permit.effect.effect_id,
                    completion.outcome,
                    completion.attempt_count,
                )
            )
        return tuple(reports)


def _normalize_handler_result(value: object) -> HandlerResult:
    """Keep only typed receipts and allowlisted fixed failure codes."""

    if not isinstance(value, HandlerResult) or type(value.success) is not bool:
        return HandlerResult(
            success=False,
            error_code=HandlerErrorCode.INVALID_HANDLER_RESULT,
        )
    if not value.success:
        if value.error_code is None:
            code = HandlerErrorCode.HANDLER_FAILED
        elif isinstance(value.error_code, HandlerErrorCode):
            code = value.error_code
        else:
            code = HandlerErrorCode.INVALID_HANDLER_RESULT
        return HandlerResult(success=False, error_code=code)
    try:
        receipt = parse_effect_success_receipt(
            {"receipt_schema": "effect-success/v1", "outcome_code": "completed"}
            if value.result_summary is None
            else value.result_summary
        )
    except ValueError:
        return HandlerResult(
            success=False,
            error_code=HandlerErrorCode.INVALID_HANDLER_RESULT,
        )
    return HandlerResult(success=True, result_summary=receipt)


__all__ = ["DeliveryReport", "Handler", "HandlerResult", "OutboxWorker"]
