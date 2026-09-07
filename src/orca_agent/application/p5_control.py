"""Short, fenced observation/cancellation effects for the shared outbox worker."""

from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import CommandId, new_id
from orca_agent.domain.p5 import P5Phase
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.infrastructure.worker import HandlerResult, OutboxWorker
from orca_agent.orchestration.dispatch_policy import P5_EFFECT_REGISTRY
from orca_agent.orchestration.effects import EffectClass, EffectSpec
from orca_agent.orchestration.p5_commands import P5CommandType, P5EventType

from .p5_dispatch import P5EffectCompletion

CONTROL_TYPES = {"internal.p5.observe_job", "external.p5.cancel_job"}


def control_effect(execution_id, *, cancel=False):
    return EffectSpec(
        effect_index=0,
        effect_type="external.p5.cancel_job" if cancel else "internal.p5.observe_job",
        effect_class=EffectClass.EXTERNAL if cancel else EffectClass.INTERNAL,
        payload={"execution_id": str(execution_id)},
    ).model_dump(mode="json")


def deliver_control(service, run_id, *, enqueue=True):
    """Durable observation, no numerical launch capability, no lease-length wait."""
    if enqueue:
        with SQLiteUnitOfWork(service.database_path, clock=service.clock) as uow:
            uow.begin()
            snapshot = service._verified_snapshot(uow, run_id)
            state = snapshot.state
            pending = any(
                effect.effect_type in CONTROL_TYPES
                and effect.status.value in {"pending", "leased", "dispatching"}
                for effect in uow.outbox.list_for_run(run_id)
            )
            if state.current_execution_id is None or state.phase in {
                P5Phase.COMPLETED,
                P5Phase.FAILED,
                P5Phase.CANCELLED,
            }:
                uow.commit()
                return None
            if not pending:
                details = {"effects": [control_effect(state.current_execution_id)]}
                service._append_event(
                    uow,
                    snapshot=snapshot,
                    next_state=state,
                    command_id=new_id(CommandId),
                    command_hash=sha256_hex(details),
                    command_type=P5CommandType.RECONCILE_EXECUTION,
                    event_type=P5EventType.RECONCILED,
                    outcome_code="observation_queued",
                    now=service.clock.now_utc(),
                    details=details,
                )
            uow.commit()
    results = []

    def handle(permit):
        with SQLiteUnitOfWork(service.database_path, clock=service.clock) as uow:
            uow.begin()
            uow.outbox.validate_handler_permit(permit=permit, now=service.clock.now_utc())
            snapshot = service._verified_snapshot(uow, run_id)
            execution = permit.effect.payload.get("execution_id")
            if execution != str(snapshot.state.current_execution_id):
                # A prior delivery may already have collected it. Never reopen.
                uow.commit()
                return HandlerResult(success=True)
            uow.commit()
        service._restore_execution_runtime(run_id)
        if permit.effect.effect_type == "external.p5.cancel_job":
            service.backend.cancel(execution, str(permit.effect.effect_id))
        # Acknowledge the short OS observation before the business collector
        # transitions the run to terminal. This preserves the shared invariant
        # that terminal runs cannot retain dispatching effects. A crash between
        # acknowledgement and collection is recovered by a new observation,
        # never by relaunching the numerical job.
        service.backend.poll(execution)
        results.append(execution)
        return HandlerResult(success=True)

    deliveries = OutboxWorker(
        service.database_path,
        handler=handle,
        clock=service.clock,
        registry=P5_EFFECT_REGISTRY,
        completion_service_factory=lambda: P5EffectCompletion(service),
        readiness_check=lambda effect, _snapshot, _now: (
            effect.run_id == run_id and effect.effect_type in CONTROL_TYPES
        ),
    ).run_once(limit=1)
    if results and deliveries and deliveries[-1].outcome == "succeeded":
        return service.reconcile(run_id)
    return None
