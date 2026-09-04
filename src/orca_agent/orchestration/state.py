"""Small, serializable run state owned by the P2 reducer."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator

from orca_agent.domain.ids import InterruptId, RunId

from .codes import CancelReasonCode


class KernelModel(BaseModel):
    """Strict immutable model base for the durable kernel."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class RunStatus(StrEnum):
    CREATED = "created"
    WAITING_FOR_INPUT = "waiting_for_input"
    READY = "ready"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (RunStatus.CANCELLED, RunStatus.FAILED)


class KernelState(KernelModel):
    """The business state reconstructed by replaying kernel events."""

    run_id: RunId
    status: RunStatus
    pending_interrupt_id: InterruptId | None
    last_outcome_code: str | None
    cancel_reason_code: CancelReasonCode | None

    @classmethod
    def created(cls, run_id: RunId) -> KernelState:
        return cls(
            run_id=run_id,
            status=RunStatus.CREATED,
            pending_interrupt_id=None,
            last_outcome_code="run_created",
            cancel_reason_code=None,
        )

    @field_validator("last_outcome_code", "cancel_reason_code")
    @classmethod
    def _blank_codes_are_not_valid(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("outcome and cancel codes must not be blank")
        return value


RunState = KernelState

__all__ = ["KernelModel", "KernelState", "RunState", "RunStatus"]
