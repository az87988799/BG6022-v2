"""One-shot outbox worker with an injected, side-effect-free test handler."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from orca_agent.application.errors import LeaseLostError, StorageError
from orca_agent.domain.ids import EffectId, WorkerId, new_id

from .clock import Clock, SystemClock
from .outbox import LeasedEffect, OutboxStatus
from .unit_of_work import SQLiteUnitOfWork


@dataclass(frozen=True)
class HandlerResult:
    success: bool
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class DeliveryReport:
    effect_id: EffectId
    outcome: str
    attempt_count: int


Handler = Callable[[LeasedEffect], HandlerResult]


class OutboxWorker:
    """Claim a bounded batch, invoke an injected handler, and record delivery."""

    def __init__(
        self,
        database_path: str | Path,
        handler: Handler,
        *,
        clock: Clock | None = None,
        worker_id: WorkerId | None = None,
        lease_duration: timedelta = timedelta(seconds=30),
        max_attempts: int = 5,
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.database_path = database_path
        self.handler = handler
        self.clock = clock or SystemClock()
        self.worker_id = worker_id or new_id(WorkerId)
        self.lease_duration = lease_duration
        self.max_attempts = max_attempts

    def run_once(self, *, limit: int = 1) -> tuple[DeliveryReport, ...]:
        """Deliver at most ``limit`` effects; no background loop is created."""

        if limit < 1:
            return ()
        now = self.clock.now_utc()
        with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
            if uow.outbox is None:
                raise StorageError("outbox repository is unavailable")
            claimed = uow.outbox.claim_due(
                worker_id=self.worker_id,
                now=now,
                lease_duration=self.lease_duration,
                limit=limit,
            )

        reports: list[DeliveryReport] = []
        for effect in claimed:
            try:
                result = self.handler(effect)
                if not isinstance(result, HandlerResult):
                    result = HandlerResult(success=bool(result))  # type: ignore[arg-type]
            except Exception:
                result = HandlerResult(
                    success=False,
                    error_code="handler_exception",
                    error_message="injected handler raised an exception",
                )
            if result.success:
                reports.append(self._record_success(effect))
            else:
                reports.append(self._record_failure(effect, result))
        return tuple(reports)

    def _record_success(self, effect: LeasedEffect) -> DeliveryReport:
        try:
            with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
                if uow.outbox is None:
                    raise StorageError("outbox repository is unavailable")
                uow.outbox.mark_succeeded(
                    effect_id=effect.effect_id,
                    worker_id=self.worker_id,
                    now=self.clock.now_utc(),
                )
        except LeaseLostError:
            return DeliveryReport(effect.effect_id, "lease_lost", effect.attempt_count)
        return DeliveryReport(effect.effect_id, "succeeded", effect.attempt_count)

    def _record_failure(self, effect: LeasedEffect, result: HandlerResult) -> DeliveryReport:
        error_code = result.error_code or "handler_failed"
        error_message = result.error_message or "injected handler reported failure"
        try:
            with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
                if uow.outbox is None:
                    raise StorageError("outbox repository is unavailable")
                updated = uow.outbox.mark_failed(
                    effect_id=effect.effect_id,
                    worker_id=self.worker_id,
                    now=self.clock.now_utc(),
                    error_code=error_code,
                    error_message=error_message,
                    max_attempts=self.max_attempts,
                )
        except LeaseLostError:
            return DeliveryReport(effect.effect_id, "lease_lost", effect.attempt_count)
        outcome = "dead_letter" if updated.status is OutboxStatus.DEAD_LETTER else "retry"
        return DeliveryReport(effect.effect_id, outcome, updated.attempt_count)


__all__ = ["DeliveryReport", "Handler", "HandlerResult", "OutboxWorker"]
