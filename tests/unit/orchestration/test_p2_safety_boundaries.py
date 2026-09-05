import pytest
from pydantic import ValidationError

from orca_agent.domain.ids import ArtifactId, RunId, new_id
from orca_agent.orchestration.codes import CancelReasonCode, HandlerErrorCode
from orca_agent.orchestration.dispatch_policy import (
    DEFAULT_EFFECT_REGISTRY,
    DispatchDecision,
    EffectRegistration,
    EffectRegistry,
    evaluate_dispatch,
)
from orca_agent.orchestration.effect_receipts import (
    parse_effect_success_receipt,
)
from orca_agent.orchestration.effects import EffectClass, EffectSpec
from orca_agent.orchestration.state import KernelState, RunStatus


def test_effect_success_receipt_is_typed_bounded_and_immutable() -> None:
    artifact_id = ArtifactId("artifact_00000000000000000000000000000001")
    receipt = parse_effect_success_receipt(
        {
            "receipt_schema": "effect-success/v1",
            "outcome_code": "completed",
            "artifact_ids": [str(artifact_id)],
        }
    )

    assert receipt.artifact_ids == (artifact_id,)
    assert parse_effect_success_receipt(receipt) is receipt
    with pytest.raises(ValidationError):
        receipt.artifact_ids += (artifact_id,)  # type: ignore[misc]


@pytest.mark.parametrize(
    "value",
    (
        ["not-an-object"],
        {
            "receipt_schema": "effect-success/v1",
            "outcome_code": "completed",
            "artifact_ids": [
                "artifact_00000000000000000000000000000001",
                "artifact_00000000000000000000000000000001",
            ],
        },
        {
            "receipt_schema": "effect-success/v1",
            "outcome_code": "completed",
            "artifact_ids": [],
            "unexpected": "must be rejected",
        },
        {
            "receipt_schema": "effect-success/v2",
            "outcome_code": "completed",
            "artifact_ids": [],
        },
    ),
)
def test_effect_success_receipt_rejects_untrusted_shapes(value: object) -> None:
    with pytest.raises(ValueError):
        parse_effect_success_receipt(value)


def test_effect_success_receipt_rejects_more_than_sixteen_artifacts() -> None:
    artifacts = [f"artifact_{index:032x}" for index in range(17)]
    with pytest.raises(ValueError):
        parse_effect_success_receipt(
            {
                "receipt_schema": "effect-success/v1",
                "outcome_code": "completed",
                "artifact_ids": artifacts,
            }
        )


def test_effect_registry_rejects_ambiguous_or_unsupported_definitions() -> None:
    registration = EffectRegistration(
        effect_type="internal.test",
        effect_class=EffectClass.INTERNAL,
        allowed_statuses=frozenset({RunStatus.CREATED}),
    )
    with pytest.raises(ValueError):
        EffectRegistry((registration, registration))
    with pytest.raises(ValueError):
        EffectRegistry(
            (
                EffectRegistration(
                    effect_type=" ",
                    effect_class=EffectClass.INTERNAL,
                    allowed_statuses=frozenset({RunStatus.CREATED}),
                ),
            )
        )
    with pytest.raises(ValueError):
        EffectRegistry(
            (
                EffectRegistration(
                    effect_type="internal.test",
                    effect_class=EffectClass.INTERNAL,
                    allowed_statuses=frozenset(),
                ),
            )
        )
    with pytest.raises(ValueError):
        EffectRegistry(
            (
                EffectRegistration(
                    effect_type="internal.test",
                    effect_class=EffectClass.INTERNAL,
                    allowed_statuses=frozenset({RunStatus.CREATED}),
                    receipt_schema="effect-success/v2",
                ),
            )
        )
    with pytest.raises(ValueError):
        EffectRegistry(policy_version=0)


def test_dispatch_policy_is_exact_and_fail_closed() -> None:
    run_id = new_id(RunId)
    created = KernelState.created(run_id)
    external = EffectSpec(
        effect_index=0,
        effect_type="external.test",
        effect_class=EffectClass.EXTERNAL,
        payload={},
    )
    internal = EffectSpec(
        effect_index=0,
        effect_type="internal.test",
        effect_class=EffectClass.INTERNAL,
        payload={},
    )
    safe_internal = EffectSpec(
        effect_index=0,
        effect_type="internal.audit",
        effect_class=EffectClass.INTERNAL,
        payload={},
    )

    assert evaluate_dispatch(created, external) is DispatchDecision.ALLOW
    assert (
        evaluate_dispatch(
            created, external, {"external.test": DEFAULT_EFFECT_REGISTRY["external.test"]}
        )
        is DispatchDecision.ALLOW
    )
    assert (
        evaluate_dispatch(created, internal.model_copy(update={"effect_type": "unknown"}))
        is DispatchDecision.BLOCK
    )
    assert (
        evaluate_dispatch(
            created, internal.model_copy(update={"effect_class": EffectClass.EXTERNAL})
        )
        is DispatchDecision.BLOCK
    )

    waiting = created.model_copy(update={"status": RunStatus.WAITING_FOR_INPUT})
    assert evaluate_dispatch(waiting, external) is DispatchDecision.BLOCK
    assert evaluate_dispatch(waiting, internal) is DispatchDecision.BLOCK
    assert evaluate_dispatch(waiting, safe_internal) is DispatchDecision.ALLOW

    terminal = created.model_copy(update={"status": RunStatus.CANCELLED})
    assert evaluate_dispatch(terminal, external) is DispatchDecision.CANCEL


def test_reason_and_handler_codes_are_closed() -> None:
    assert CancelReasonCode("user_cancelled") is CancelReasonCode.USER_CANCELLED
    assert HandlerErrorCode("handler_failed") is HandlerErrorCode.HANDLER_FAILED
    with pytest.raises(ValueError):
        CancelReasonCode("secret: operator input")
    with pytest.raises(ValueError):
        HandlerErrorCode("traceback: leaked diagnostic")
