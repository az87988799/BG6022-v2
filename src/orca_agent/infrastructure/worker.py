"""Fail-closed, one-shot outbox worker with fenced leases."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from orca_agent.application.errors import LeaseLostError, StorageError
from orca_agent.domain.ids import EffectId, WorkerId, new_id
from orca_agent.domain.json_types import JsonObject, freeze_json_object, thaw_json

from .clock import Clock, SystemClock
from .outbox import LeasedEffect, OutboxStatus
from .sqlite import resolve_database_path
from .unit_of_work import SQLiteUnitOfWork


@dataclass(frozen=True)
class HandlerResult:
    success: bool
    error_code: str | None = None
    error_message: str | None = None
    result_summary: object | None = None


@dataclass(frozen=True)
class DeliveryReport:
    effect_id: EffectId
    outcome: str
    attempt_count: int


Handler = Callable[[LeasedEffect], object]


class OutboxWorker:
    """Claim, verify, handle, and complete one effect at a time."""

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
        self.database_path = resolve_database_path(database_path or state_root)  # type: ignore[arg-type]
        self.handler = handler
        self.clock = clock or SystemClock()
        self.worker_id = worker_id or new_id(WorkerId)
        self.lease_duration = lease_duration
        self.max_attempts = max_attempts

    def run_once(self, *, limit: int = 1) -> tuple[DeliveryReport, ...]:
        """Deliver at most ``limit`` effects without preclaiming a stale batch."""

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
                    effect = uow.outbox.prepare_dispatch(
                        runs=uow.runs,
                        events=uow.events,
                        interrupts=uow.interrupts,
                        effect_id=claimed_effect.effect_id,
                        worker_id=self.worker_id,
                        expected_generation=claimed_effect.attempt_count,
                        now=self.clock.now_utc(),
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
            if effect is None:
                reports.append(
                    DeliveryReport(
                        claimed_effect.effect_id,
                        "lease_lost",
                        claimed_effect.attempt_count,
                    )
                )
                continue

            try:
                result = _normalize_handler_result(self.handler(effect))
            except Exception:
                result = HandlerResult(
                    success=False,
                    error_code="handler_exception",
                    error_message="injected handler raised an exception",
                )
            if result.success:
                reports.append(self._record_success(effect, result))
            else:
                reports.append(self._record_failure(effect, result))
        return tuple(reports)

    def _record_success(self, effect: LeasedEffect, result: HandlerResult) -> DeliveryReport:
        summary = result.result_summary
        if not isinstance(summary, dict):
            summary = thaw_json(freeze_json_object(summary or {}))
        try:
            with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
                if uow.outbox is None:
                    raise StorageError("outbox repository is unavailable")
                uow.outbox.mark_succeeded(
                    effect_id=effect.effect_id,
                    worker_id=self.worker_id,
                    expected_generation=effect.attempt_count,
                    now=self.clock.now_utc(),
                    result_summary=summary,
                )
        except LeaseLostError:
            return DeliveryReport(effect.effect_id, "lease_lost", effect.attempt_count)
        return DeliveryReport(effect.effect_id, "succeeded", effect.attempt_count)

    def _record_failure(self, effect: LeasedEffect, result: HandlerResult) -> DeliveryReport:
        try:
            with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
                if uow.outbox is None:
                    raise StorageError("outbox repository is unavailable")
                updated = uow.outbox.mark_failed(
                    effect_id=effect.effect_id,
                    worker_id=self.worker_id,
                    expected_generation=effect.attempt_count,
                    now=self.clock.now_utc(),
                    error_code=result.error_code or "handler_failed",
                    error_message=result.error_message
                    or "The handler reported a controlled failure.",
                    max_attempts=self.max_attempts,
                )
        except LeaseLostError:
            return DeliveryReport(effect.effect_id, "lease_lost", effect.attempt_count)
        outcome = "dead_letter" if updated.status is OutboxStatus.DEAD_LETTER else "retry"
        return DeliveryReport(effect.effect_id, outcome, updated.attempt_count)


def _normalize_handler_result(value: object) -> HandlerResult:
    """Fail closed and retain only fixed, non-sensitive failure summaries."""

    if not isinstance(value, HandlerResult):
        return HandlerResult(
            success=False,
            error_code="invalid_handler_result",
            error_message="injected handler returned a non-HandlerResult",
        )
    if type(value.success) is not bool:
        return HandlerResult(
            success=False,
            error_code="invalid_handler_result",
            error_message="injected handler returned an invalid HandlerResult",
        )
    if not value.success:
        return HandlerResult(
            success=False,
            error_code="handler_failed",
            error_message="The handler reported a controlled failure.",
        )
    try:
        summary: JsonObject = {}
        if value.result_summary is not None:
            summary = thaw_json(freeze_json_object(value.result_summary))  # type: ignore[assignment]
    except (TypeError, ValueError):
        return HandlerResult(
            success=False,
            error_code="invalid_handler_result",
            error_message="injected handler returned an invalid HandlerResult",
        )
    return HandlerResult(success=True, result_summary=summary)


__all__ = ["DeliveryReport", "Handler", "HandlerResult", "OutboxWorker"]
