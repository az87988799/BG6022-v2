"""P5 completion adapter for the shared fenced outbox worker."""

from orca_agent.application.effect_completion import EffectCompletionReport
from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import completion_command_id
from orca_agent.infrastructure.outbox import OutboxStatus
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.orchestration.codes import HandlerErrorCode, handler_error_message
from orca_agent.orchestration.effect_receipts import parse_effect_success_receipt, receipt_json
from orca_agent.orchestration.p5_commands import P5CommandType, P5EventType


class P5EffectCompletion:
    """Atomically bind the shared permit's terminal receipt to the P5 event stream."""

    def __init__(self, service):
        self.service = service

    def complete(self, permit, result):
        outcome = "succeeded" if result.success else "dead_letter"
        command_id = completion_command_id(permit.effect.effect_id, permit.generation, outcome)
        with SQLiteUnitOfWork(self.service.database_path, clock=self.service.clock) as uow:
            uow.begin()
            snapshot = self.service._verified_snapshot(uow, permit.effect.run_id)
            existing = uow.events.get_by_command_id(command_id)
            if existing is not None:
                uow.commit()
                return EffectCompletionReport(permit.effect.effect_id, outcome, permit.generation)
            uow.outbox.validate_dispatch_permit(permit=permit, now=self.service.clock.now_utc())
            receipt = (
                parse_effect_success_receipt(
                    {"receipt_schema": "effect-success/v1", "outcome_code": "completed"}
                )
                if result.success
                else None
            )
            error = (
                None if result.success else (result.error_code or HandlerErrorCode.HANDLER_FAILED)
            )
            details = {"effect_id": str(permit.effect.effect_id), "generation": permit.generation}
            if receipt is not None:
                details["result_summary"] = receipt_json(receipt)
            else:
                details.update(error_code=error.value, error_message=handler_error_message(error))
            event, app_result = self.service._append_event(
                uow,
                snapshot=snapshot,
                next_state=snapshot.state,
                command_id=command_id,
                command_hash=sha256_hex(details),
                command_type=P5CommandType.COMPLETE_EFFECT,
                event_type=P5EventType.EFFECT_SUCCEEDED
                if result.success
                else P5EventType.EFFECT_DEAD_LETTERED,
                outcome_code=outcome,
                now=self.service.clock.now_utc(),
                details=details,
            )
            uow.outbox.complete_terminal_in_transaction(
                permit=permit,
                status=OutboxStatus.SUCCEEDED if result.success else OutboxStatus.DEAD_LETTER,
                now=self.service.clock.now_utc(),
                audit_event_id=event.event_id,
                result_summary=receipt,
                error_code=error,
            )
            uow.commit()
            return EffectCompletionReport(
                permit.effect.effect_id, outcome, permit.generation, app_result
            )
