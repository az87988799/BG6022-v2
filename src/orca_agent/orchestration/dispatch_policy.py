"""Pure, fail-closed policy for durable effect dispatch."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType

from orca_agent.domain.hashing import sha256_hex

from .effects import EffectClass
from .state import KernelState, RunStatus


class DispatchDecision(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"
    CANCEL = "cancel"


@dataclass(frozen=True)
class EffectRegistration:
    """The immutable routing contract for one exact effect type."""

    effect_type: str
    effect_class: EffectClass
    allowed_statuses: frozenset[RunStatus]
    safe_while_waiting: bool = False
    receipt_schema: str = "effect-success/v1"


@dataclass(frozen=True, init=False)
class EffectRegistry:
    """Exact effect-type registry used by both claim and authorization."""

    _registrations: Mapping[str, EffectRegistration]
    policy_version: int

    def __init__(
        self,
        registrations: Iterable[EffectRegistration] | None = None,
        *,
        policy_version: int = 1,
    ) -> None:
        if type(policy_version) is not int or policy_version < 1:
            raise ValueError("policy_version must be a positive integer")
        expected = P2_POLICY_RULES.get(policy_version)
        if expected is None:
            raise ValueError("unknown fixed P2 policy version")
        if registrations is None:
            registrations = expected
        values: dict[str, EffectRegistration] = {}
        for registration in registrations:
            if registration.effect_type in values:
                raise ValueError("effect type is registered more than once")
            if not registration.effect_type.strip():
                raise ValueError("effect type must not be blank")
            if not registration.allowed_statuses:
                raise ValueError("effect registration must allow at least one run status")
            if registration.receipt_schema != "effect-success/v1":
                raise ValueError("effect registration has an unsupported receipt schema")
            values[registration.effect_type] = replace(
                registration, allowed_statuses=frozenset(registration.allowed_statuses)
            )
        if values != {item.effect_type: item for item in expected}:
            raise ValueError("policy version is bound to different rules")
        object.__setattr__(self, "_registrations", MappingProxyType(values))
        object.__setattr__(self, "policy_version", policy_version)

    @property
    def configuration_hash(self) -> str:
        return sha256_hex(
            [
                {
                    "type": key,
                    "class": value.effect_class.value,
                    "statuses": sorted(status.value for status in value.allowed_statuses),
                    "safe_while_waiting": value.safe_while_waiting,
                    "receipt_schema": value.receipt_schema,
                }
                for key, value in sorted(self._registrations.items())
            ]
        )

    def get(self, effect_type: str) -> EffectRegistration | None:
        return self._registrations.get(effect_type)

    def __contains__(self, effect_type: object) -> bool:
        return effect_type in self._registrations

    def __getitem__(self, effect_type: str) -> EffectRegistration:
        return self._registrations[effect_type]

    def items(self):
        return self._registrations.items()


_V1_RULES = (
    EffectRegistration(
        effect_type="external.test",
        effect_class=EffectClass.EXTERNAL,
        allowed_statuses=frozenset({RunStatus.CREATED, RunStatus.READY}),
    ),
    EffectRegistration(
        effect_type="external.audit",
        effect_class=EffectClass.EXTERNAL,
        allowed_statuses=frozenset({RunStatus.CREATED, RunStatus.READY}),
    ),
    EffectRegistration(
        effect_type="internal.test",
        effect_class=EffectClass.INTERNAL,
        allowed_statuses=frozenset({RunStatus.CREATED, RunStatus.READY}),
    ),
    EffectRegistration(
        effect_type="internal.audit",
        effect_class=EffectClass.INTERNAL,
        allowed_statuses=frozenset(
            {RunStatus.CREATED, RunStatus.WAITING_FOR_INPUT, RunStatus.READY}
        ),
        safe_while_waiting=True,
    ),
)

# Closed P2 configurations, not a hot-reload or plugin mechanism. Version 2
# explicitly permits internal.test while waiting; external dispatch stays blocked.
P2_POLICY_RULES = MappingProxyType(
    {
        1: _V1_RULES,
        2: tuple(
            replace(
                item,
                safe_while_waiting=True,
                allowed_statuses=item.allowed_statuses | {RunStatus.WAITING_FOR_INPUT},
            )
            if item.effect_type == "internal.test"
            else item
            for item in _V1_RULES
        ),
    }
)
DEFAULT_EFFECT_REGISTRY = EffectRegistry()


def _registration_for(effect_type: str, registry: EffectRegistry | Mapping[str, object]):
    if isinstance(registry, EffectRegistry):
        return registry.get(effect_type)
    return registry.get(effect_type)


def evaluate_dispatch(
    state: KernelState,
    effect: object,
    registry: EffectRegistry | Mapping[str, object] = DEFAULT_EFFECT_REGISTRY,
) -> DispatchDecision:
    """Return the only dispatch decision allowed by the current state/policy.

    The effect payload is deliberately not consulted.  Routing permissions are
    owned by this registry, never by data supplied inside an effect itself.
    """

    effect_type = getattr(effect, "effect_type", None)
    effect_class = getattr(effect, "effect_class", None)
    registration = (
        _registration_for(effect_type, registry) if isinstance(effect_type, str) else None
    )
    if registration is None:
        return DispatchDecision.BLOCK
    if getattr(registration, "effect_type", None) != effect_type:
        return DispatchDecision.BLOCK
    if getattr(registration, "effect_class", None) is not effect_class:
        return DispatchDecision.BLOCK
    if state.status in (RunStatus.CANCELLED, RunStatus.FAILED):
        return DispatchDecision.CANCEL
    allowed_statuses = getattr(registration, "allowed_statuses", frozenset())
    if state.status not in allowed_statuses:
        return DispatchDecision.BLOCK
    if state.status is RunStatus.WAITING_FOR_INPUT:
        if effect_class is EffectClass.EXTERNAL:
            return DispatchDecision.BLOCK
        if not getattr(registration, "safe_while_waiting", False):
            return DispatchDecision.BLOCK
    return DispatchDecision.ALLOW


__all__ = [
    "DEFAULT_EFFECT_REGISTRY",
    "DispatchDecision",
    "EffectRegistration",
    "EffectRegistry",
    "evaluate_dispatch",
]
