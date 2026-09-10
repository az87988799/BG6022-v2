"""Pure P7 draft resolution for the sparse v4 intake contract.

This module deliberately has no database or downstream-service dependency.
It turns a current-turn patch plus an optional existing request into a new
immutable request.  The caller remains responsible for task revisions,
planning records, and approval tokens.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from orca_agent.domain.json_types import thaw_json
from orca_agent.domain.p7_conversation import (
    MoleculeInputType,
    ParameterSource,
    ParameterValue,
)
from orca_agent.domain.p7_intake import CalculationIntentV4, FieldChangeV4, OutputPatchV4
from orca_agent.domain.p7_planning import PlanProposal
from orca_agent.domain.p7_task import (
    CalculationRequest,
    OutputKind,
    OutputQuantity,
    OutputSpec,
)
from orca_agent.planning.p7_catalog import CapabilityCatalog
from orca_agent.planning.p7_parameter_policy import (
    OXYGEN_CANONICAL_SMILES,
    MoleculePrecheck,
    MoleculePrecheckResult,
    ParameterPolicyV2,
    default_parameter_policy,
)
from orca_agent.planning.p7_plan_compiler import operations_from_proposal, proposal_from_operations


@dataclass(frozen=True)
class DraftChange:
    """Auditable projection of one accepted current-turn change."""

    field: str
    op: str
    value: object | None
    quote: str
    source: str = "current_message"

    def as_dict(self) -> dict[str, object]:
        return {
            "field": self.field,
            "op": self.op,
            "value": thaw_json(self.value),
            "source": self.source,
            "quote": self.quote,
        }


@dataclass(frozen=True)
class DraftResolution:
    """Pure result consumed by P7PlanValidator and P7TaskService."""

    request: CalculationRequest
    precheck: MoleculePrecheckResult | None
    proposal: PlanProposal | None
    changes: tuple[DraftChange, ...] = ()
    issues: tuple[str, ...] = ()
    identity_notes: tuple[str, ...] = ()
    changed: bool = False
    policy_snapshot: dict[str, object] | None = None
    raw_molecule_fragment: str | None = None


def _parameter(
    *,
    name: str,
    value: object,
    source: ParameterSource,
    turn_reference: str,
    quote: str | None = None,
    hard_constraint: bool = False,
    structure_hash: str | None = None,
    rule_version: str | None = None,
) -> ParameterValue:
    value_type = "integer" if type(value) is int else "string"
    return ParameterValue(
        name=name,
        value=value,
        value_type=value_type,
        hard_constraint=hard_constraint,
        source=source,
        source_reference=turn_reference,
        source_fragment=quote,
        structure_hash=structure_hash,
        rule_version=rule_version,
    )


def _default_output_spec(operations: tuple[str, ...]) -> OutputSpec:
    """Return the v4 minimum output contract for one exact scope."""

    quantities: list[OutputQuantity] = []
    if "opt" in operations:
        quantities.append(
            OutputQuantity(
                kind=OutputKind.OPT_FINAL_ELECTRONIC_ENERGY,
                source_selector="opt",
            )
        )
        quantities.append(OutputQuantity(kind=OutputKind.OPTIMIZED_GEOMETRY))
    if "freq" in operations:
        quantities.extend(
            (
                OutputQuantity(
                    kind=OutputKind.VIBRATIONAL_FREQUENCIES,
                    source_selector="freq",
                ),
                OutputQuantity(kind=OutputKind.LOCAL_MINIMUM_SUPPORT),
            )
        )
    if "sp" in operations:
        quantities.append(
            OutputQuantity(
                kind=OutputKind.INDEPENDENT_SP_ELECTRONIC_ENERGY,
                source_selector="sp",
            )
        )
    if not quantities:
        quantities.append(OutputQuantity(kind=OutputKind.EXECUTION_STATUS))
    return OutputSpec.create(quantities=quantities)


def _output_from_patch(
    patch: OutputPatchV4 | None,
    *,
    base: OutputSpec | None,
    operations: tuple[str, ...],
) -> OutputSpec:
    if patch is None:
        return base or _default_output_spec(operations)
    current = base or _default_output_spec(operations)
    quantities = (
        tuple(
            OutputQuantity(
                kind=OutputKind(item.kind),
                required=item.required,
                source_selector=item.source_selector,
                unit=item.unit,
                label=item.label,
            )
            for item in patch.quantities
        )
        if patch.quantities is not None
        else current.quantities
    )
    units = thaw_json(patch.units) if patch.units is not None else thaw_json(current.units)
    if not isinstance(units, dict):
        raise ValueError("output_patch.units must be an object")
    return OutputSpec.create(
        quantities=quantities,
        language=patch.language or current.language,
        style=patch.style or current.style,
        layout=patch.layout or current.layout,
        units=units,
        precision=patch.precision if patch.precision is not None else current.precision,
        artifacts=patch.artifacts if patch.artifacts is not None else current.artifacts,
        include_evidence=(
            patch.include_evidence
            if patch.include_evidence is not None
            else current.include_evidence
        ),
    )


def _proposal_from_patch(
    intent: CalculationIntentV4,
    *,
    operations: tuple[str, ...],
    catalog: CapabilityCatalog,
    current_request: CalculationRequest | None,
    current_message: str,
) -> tuple[PlanProposal | None, tuple[str, ...]]:
    issues: list[str] = []
    raw = thaw_json(intent.plan_proposal)
    if raw is not None:
        try:
            proposal = PlanProposal.model_validate_json(
                json.dumps(raw, ensure_ascii=False, separators=(",", ":")), strict=True
            )
            if not proposal.steps and not proposal.clarification_fields:
                proposal = proposal.model_copy(
                    update={"clarification_fields": ("execution_scope",)}
                )
            return proposal, ()
        except (TypeError, ValueError) as error:
            issues.append(f"invalid candidate plan: {str(error)[:512]}")
    if current_request is not None and intent.action == "revise_draft":
        effective = operations or tuple(current_request.operations)
    else:
        effective = operations or ("opt",)
    # A bare new request still gets the deterministic default Opt candidate.
    # A revision with no scope retains the current candidate through its exact
    # request; the service can avoid creating a revision when nothing changed.
    proposal = proposal_from_operations(
        effective,
        goal=current_message,
        requested_outputs=tuple(
            item.kind.value for item in _default_output_spec(effective).quantities
        ),
        action=("revise_draft" if intent.action == "revise_draft" else "plan_new"),
    )
    if not effective:
        proposal = proposal.model_copy(update={"clarification_fields": ("execution_scope",)})
    # Exercise the same closed catalog at the pure boundary so an invalid
    # model proposal is represented as a clarification rather than repaired.
    try:
        if proposal.steps:
            operations_from_proposal(proposal, catalog)
    except (TypeError, ValueError) as error:
        issues.append(f"candidate capability is invalid: {str(error)[:512]}")
    return proposal, tuple(issues)


def _current_quote(change: FieldChangeV4, current_message: str) -> str | None:
    quote = change.evidence.quote.strip()
    return quote if quote.casefold() in current_message.casefold() else None


def _change_value(change: FieldChangeV4) -> object:
    return thaw_json(change.value)


def _evidence_is_negative(quote: str, current_message: str) -> bool:
    folded = current_message.casefold()
    start = folded.find(quote.casefold())
    if start < 0:
        return False
    prefix = folded[max(0, start - 32) : start]
    return bool(
        re.search(
            r"(?:不使用|不采用|不选择|不指定|不要|无需|不必|禁止|排除|不是|"
            r"不改成|不改为|不想用|without|do\s+not|don't|not)\s*$",
            prefix,
            re.IGNORECASE,
        )
    )


def _removed_operations(current_message: str) -> frozenset[str]:
    """Extract explicit operation removals for context-aware sparse merges."""

    removed: set[str] = set()
    if re.search(
        r"(?:不要|不做|不需要|不要求|去掉|删除|移除)\s*(?:频率|振动|freq|frequency)|"
        r"(?:without|remove|drop)\s+(?:the\s+)?(?:freq|frequency|vibrational)",
        current_message,
        re.IGNORECASE,
    ):
        removed.add("freq")
    if re.search(
        r"(?:不要|不做|不需要|不要求|去掉|删除|移除)\s*(?:单点|sp|single\s+point)|"
        r"(?:without|remove|drop)\s+(?:the\s+)?(?:sp|single\s+point)",
        current_message,
        re.IGNORECASE,
    ):
        removed.add("sp")
    if re.search(
        r"(?:不要|不做|不需要|不要求|去掉|删除|移除)\s*(?:优化|opt|optimization)|"
        r"(?:without|remove|drop)\s+(?:the\s+)?(?:opt|optimization)",
        current_message,
        re.IGNORECASE,
    ):
        removed.add("opt")
    return frozenset(removed)


def _evidence_supports_change(
    change: FieldChangeV4,
    value: object,
    quote: str,
    *,
    policy: ParameterPolicyV2,
) -> bool:
    """Require the current quote to refer to the field it is changing."""

    folded = quote.casefold()
    if change.field in {"method", "environment"}:
        expected = str(value).casefold()
        if change.field == "method" and expected.replace("²", "2").replace("-", "") == "r2scan3c":
            return bool(re.search(r"r\s*[²2]?\s*scan\s*[- ]?\s*3c", quote, re.I))
        if change.field == "environment" and expected in {"gas", "gas-phase", "gas phase"}:
            return any(item in folded for item in ("gas", "气相", "气体"))
        if change.field == "environment" and expected in {
            "solvent",
            "solution",
            "aqueous",
        }:
            return any(item in folded for item in ("solvent", "solution", "aqueous", "溶液"))
        return expected in folded
    if change.field in {"charge", "multiplicity"}:
        # The sign is part of the semantic value.  An optional-sign match
        # would accept evidence for the opposite electronic state.
        integer = int(value)
        signed_value = rf"-{abs(integer)}" if integer < 0 else rf"\+?{abs(integer)}"
        return bool(re.search(rf"(?<![\d+-]){signed_value}(?!\d)", quote))
    if change.field == "molecule" and isinstance(value, Mapping):
        kind = value.get("kind")
        raw_value = value.get("value")
        if not isinstance(kind, str) or not isinstance(raw_value, str):
            return False
        if kind == "name":
            molecule = policy.molecule_for(MoleculeInputType.NAME, raw_value)
            names = (
                (molecule.canonical_name, *molecule.aliases)
                if molecule is not None
                else {"benzene": ("benzene", "苯")}.get(raw_value.casefold(), (raw_value,))
            )
            return any(item.casefold() in folded for item in names)
        return raw_value.casefold() in folded
    if change.field == "operations" and isinstance(value, list):
        normalized_operations = {str(item).casefold() for item in value}
        removed = _removed_operations(quote)
        if removed and removed.isdisjoint(normalized_operations):
            return True
        if normalized_operations == {"opt", "freq", "sp"} and any(
            item in folded for item in ("完整", "全流程", "complete workflow", "all steps")
        ):
            return True
        if normalized_operations == {"opt", "freq"} and any(
            item in folded for item in ("最低", "局部极小", "local minimum")
        ):
            return True
        markers = {
            "opt": ("opt", "优化"),
            "freq": ("freq", "frequency", "频率", "振动"),
            "sp": ("sp", "single point", "单点"),
        }
        return all(
            any(marker in folded for marker in markers.get(str(item).casefold(), (str(item),)))
            for item in value
        )
    return True


def _same_as_current(
    change: FieldChangeV4, value: object, current_request: CalculationRequest
) -> bool:
    """Ignore a model-repeated value when it is not a semantic change."""

    if change.op != "set":
        return False
    current: object | None
    if change.field == "molecule":
        current = {
            "kind": None
            if current_request.molecule_kind is None
            else current_request.molecule_kind.value,
            "value": current_request.molecule_value,
        }
        if isinstance(value, Mapping):
            return value.get("kind") == current["kind"] and value.get("value") == current["value"]
        return False
    if change.field == "molecule_kind":
        current = (
            None if current_request.molecule_kind is None else current_request.molecule_kind.value
        )
    elif change.field == "molecule_value":
        current = current_request.molecule_value
    elif change.field in {"method", "environment", "charge", "multiplicity"}:
        parameter = getattr(current_request, change.field)
        if parameter is None:
            return False
        current = None if parameter is None else thaw_json(parameter.value)
        if current == value and parameter.source not in {
            ParameterSource.USER_EXPLICIT,
            ParameterSource.USER_ACCEPTED_RECOMMENDATION,
        }:
            # Explicitly repeating a program recommendation (or a
            # structure-derived value) upgrades its provenance to a user
            # constraint; repeating an already explicit value is a no-op.
            return False
    elif change.field == "operations":
        current = list(current_request.operations)
        return tuple(value) == tuple(current) if isinstance(value, (list, tuple)) else False
    elif change.field == "hard_constraint":
        return isinstance(value, str) and value in current_request.hard_constraints
    elif change.field == "prohibited_request":
        return isinstance(value, str) and value in current_request.prohibited_requests
    else:
        return False
    return value == current


def _known_policy_molecule(
    policy: ParameterPolicyV2,
    kind: MoleculeInputType | None,
    value: str | None,
):
    if kind is None or value is None:
        return None
    return policy.molecule_for(kind, value)


def _molecule_fields(
    value: object,
) -> tuple[MoleculeInputType, str, str | None]:
    if not isinstance(value, Mapping):
        raise ValueError("molecule change must be an object")
    kind = MoleculeInputType(str(value.get("kind")))
    raw_value = value.get("value")
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise ValueError("molecule change value is blank")
    raw = value.get("raw")
    if raw is not None and not isinstance(raw, str):
        raise ValueError("molecule raw fragment must be a string")
    return kind, raw_value.strip(), raw


def _apply_policy_defaults(
    *,
    policy: ParameterPolicyV2,
    turn_reference: str,
    molecule_kind: MoleculeInputType | None,
    molecule_value: str | None,
    charge: ParameterValue | None,
    multiplicity: ParameterValue | None,
    precheck: MoleculePrecheckResult | None,
    explicit_charge: bool,
    explicit_multiplicity: bool,
    issues: list[str],
) -> tuple[ParameterValue | None, ParameterValue | None, tuple[str, ...]]:
    notes: list[str] = []
    molecule = _known_policy_molecule(policy, molecule_kind, molecule_value)
    if precheck is not None and precheck.status == "valid":
        if charge is None and precheck.formal_charge is not None:
            charge = _parameter(
                name="charge",
                value=precheck.formal_charge,
                source=ParameterSource.STRUCTURE_DERIVED,
                turn_reference=turn_reference,
                structure_hash=precheck.structure_hash,
            )
        if multiplicity is None and precheck.canonical_isomeric_smiles == OXYGEN_CANONICAL_SMILES:
            issues.extend(("multiplicity_required", "oxygen_triplet_requires_separate_policy"))
            return charge, multiplicity, tuple(notes)
        if multiplicity is None:
            molecule = molecule or policy.molecule_for_smiles(
                precheck.canonical_isomeric_smiles or ""
            )
        if molecule is not None:
            if charge is None and not explicit_charge:
                charge = _parameter(
                    name="charge",
                    value=molecule.charge,
                    source=ParameterSource.POLICY_RECOMMENDED,
                    turn_reference=turn_reference,
                    structure_hash=precheck.structure_hash,
                    rule_version=molecule.rule_id,
                )
            if multiplicity is None and not explicit_multiplicity:
                multiplicity = _parameter(
                    name="multiplicity",
                    value=molecule.multiplicity,
                    source=ParameterSource.POLICY_RECOMMENDED,
                    turn_reference=turn_reference,
                    structure_hash=precheck.structure_hash,
                    rule_version=molecule.rule_id,
                )
                notes.append(
                    f"{molecule.canonical_name} 使用策略 {molecule.rule_id} 推荐的中性闭壳层单重态"
                )
        elif multiplicity is None:
            issues.append("multiplicity_required")
    elif molecule is not None:
        if charge is None and not explicit_charge:
            charge = _parameter(
                name="charge",
                value=molecule.charge,
                source=ParameterSource.POLICY_RECOMMENDED,
                turn_reference=turn_reference,
                rule_version=molecule.rule_id,
            )
        if multiplicity is None and not explicit_multiplicity:
            multiplicity = _parameter(
                name="multiplicity",
                value=molecule.multiplicity,
                source=ParameterSource.POLICY_RECOMMENDED,
                turn_reference=turn_reference,
                rule_version=molecule.rule_id,
            )
            notes.append(
                f"{molecule.canonical_name} 使用策略 {molecule.rule_id} 推荐的中性闭壳层单重态"
            )
    return charge, multiplicity, tuple(notes)


def resolve_draft(
    *,
    current_request: CalculationRequest | None,
    current_planning_record: object | None,
    user_patch: CalculationIntentV4 | Mapping[str, object],
    current_message: str,
    turn_reference: str,
    default_policy: ParameterPolicyV2 | None = None,
    capability_catalog: CapabilityCatalog,
) -> DraftResolution:
    """Resolve a sparse v4 patch without mutating history or calling services."""

    intent = (
        user_patch
        if isinstance(user_patch, CalculationIntentV4)
        else CalculationIntentV4.model_validate_json(
            json.dumps(thaw_json(user_patch), ensure_ascii=False, separators=(",", ":")),
            strict=True,
        )
    )
    policy = default_policy or default_parameter_policy()
    issues: list[str] = []
    notes: list[str] = []
    accepted_changes: list[DraftChange] = []
    changes = tuple(intent.changes)

    if current_request is None:
        molecule_kind = None
        molecule_value = None
        charge = None
        multiplicity = None
        method = None
        environment = None
        operations: tuple[str, ...] = ()
        hard_constraints: tuple[str, ...] = ()
        prohibited_requests: tuple[str, ...] = ()
        base_output = None
    else:
        molecule_kind = current_request.molecule_kind
        molecule_value = current_request.molecule_value
        charge = current_request.charge
        multiplicity = current_request.multiplicity
        method = current_request.method
        environment = current_request.environment
        operations = tuple(current_request.operations)
        hard_constraints = tuple(current_request.hard_constraints)
        prohibited_requests = tuple(current_request.prohibited_requests)
        base_output = current_request.output_spec

    molecule_changed = False
    explicit_charge = False
    explicit_multiplicity = False
    unreliable_change = False
    output_patch = intent.output_patch
    raw_molecule_fragment: str | None = None

    for change in changes:
        quote = _current_quote(change, current_message)
        if quote is None:
            unreliable_change = True
            issues.append(f"change_evidence_not_in_current_message:{change.field}")
            continue
        value = _change_value(change)
        if change.field == "operations" and isinstance(value, list):
            removed = _removed_operations(current_message)
            if removed and current_request is not None:
                base_operations = tuple(current_request.operations)
                value = [item for item in base_operations if item not in removed]
        if change.op == "set" and (
            _evidence_is_negative(quote, current_message)
            or not _evidence_supports_change(change, value, quote, policy=policy)
        ):
            unreliable_change = True
            issue = (
                f"negative_change_evidence:{change.field}"
                if _evidence_is_negative(quote, current_message)
                else f"change_evidence_field_mismatch:{change.field}"
            )
            issues.append(issue)
            continue
        if current_request is not None and _same_as_current(change, value, current_request):
            # A model may repeat the current value while explaining the
            # draft.  Repetition is not a user change and must not create a
            # new revision or replace its provenance.
            continue
        accepted_changes.append(DraftChange(change.field, change.op, value, quote))
        if change.op == "reset_default":
            if change.field == "method":
                method = None
            elif change.field == "environment":
                environment = None
            elif change.field == "charge":
                charge = None
            elif change.field == "multiplicity":
                multiplicity = None
            elif change.field == "operations":
                operations = ()
            else:
                unreliable_change = True
                issues.append(f"reset_default_not_supported:{change.field}")
            continue
        if change.field == "molecule":
            try:
                molecule_kind, molecule_value, raw_molecule_fragment = _molecule_fields(value)
            except (TypeError, ValueError) as error:
                unreliable_change = True
                issues.append(f"invalid_molecule_change:{str(error)[:256]}")
                continue
            raw_molecule_fragment = raw_molecule_fragment or quote
            molecule_changed = True
        elif change.field == "molecule_kind":
            try:
                molecule_kind = MoleculeInputType(str(value))
                molecule_changed = True
            except ValueError:
                unreliable_change = True
                issues.append("invalid_molecule_kind")
        elif change.field == "molecule_value":
            if not isinstance(value, str) or not value.strip():
                unreliable_change = True
                issues.append("invalid_molecule_value")
            else:
                molecule_value = value.strip()
                molecule_changed = True
        elif change.field == "method":
            method = _parameter(
                name="method",
                value=str(value),
                source=ParameterSource.USER_EXPLICIT,
                turn_reference=turn_reference,
                quote=quote,
                hard_constraint=True,
            )
        elif change.field == "environment":
            environment = _parameter(
                name="environment",
                value=str(value),
                source=ParameterSource.USER_EXPLICIT,
                turn_reference=turn_reference,
                quote=quote,
                hard_constraint=True,
            )
        elif change.field == "charge":
            explicit_charge = True
            charge = _parameter(
                name="charge",
                value=value,
                source=ParameterSource.USER_EXPLICIT,
                turn_reference=turn_reference,
                quote=quote,
                hard_constraint=True,
            )
        elif change.field == "multiplicity":
            explicit_multiplicity = True
            multiplicity = _parameter(
                name="multiplicity",
                value=value,
                source=ParameterSource.USER_EXPLICIT,
                turn_reference=turn_reference,
                quote=quote,
                hard_constraint=True,
            )
        elif change.field == "operations":
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                unreliable_change = True
                issues.append("invalid_operations")
            else:
                operations = tuple(item.casefold().strip() for item in value)
        elif change.field == "hard_constraint":
            hard_constraints = tuple(dict.fromkeys((*hard_constraints, str(value))))
        elif change.field == "prohibited_request":
            prohibited_requests = tuple(dict.fromkeys((*prohibited_requests, str(value))))

    if molecule_changed:
        # q/M from the previous identity are never inherited across an
        # identity change.  Explicit q/M changes in this same turn are
        # retained and policy is applied only to omissions.
        if not explicit_charge:
            charge = None
        if not explicit_multiplicity:
            multiplicity = None

    if molecule_kind is None and molecule_value is not None:
        issues.append("molecule_kind_required")
    if molecule_kind is not None and molecule_value is None:
        issues.append("molecule_value_required")

    precheck: MoleculePrecheckResult | None = None
    if molecule_kind is MoleculeInputType.SMILES and molecule_value is not None:
        precheck = MoleculePrecheck(policy=policy).check(
            input_kind=molecule_kind,
            raw_input=molecule_value,
            charge=None if charge is None else int(charge.value),
        )
        if precheck.status != "valid":
            issues.append(precheck.reason_code or "invalid_structure")

    charge, multiplicity, policy_notes = _apply_policy_defaults(
        policy=policy,
        turn_reference=turn_reference,
        molecule_kind=molecule_kind,
        molecule_value=molecule_value,
        charge=charge,
        multiplicity=multiplicity,
        precheck=precheck,
        explicit_charge=explicit_charge,
        explicit_multiplicity=explicit_multiplicity,
        issues=issues,
    )
    notes.extend(policy_notes)

    if method is None:
        recommendation = policy.recommendation("method")
        method = _parameter(
            name="method",
            value=recommendation.value,
            source=ParameterSource.POLICY_RECOMMENDED,
            turn_reference=turn_reference,
            rule_version=recommendation.rule_id,
        )
    if environment is None:
        recommendation = policy.recommendation("environment")
        environment = _parameter(
            name="environment",
            value=recommendation.value,
            source=ParameterSource.POLICY_RECOMMENDED,
            turn_reference=turn_reference,
            rule_version=recommendation.rule_id,
        )

    if intent.action == "revise_draft" and current_request is not None and not operations:
        operations = tuple(current_request.operations)
    proposal, proposal_issues = _proposal_from_patch(
        intent,
        operations=operations,
        catalog=capability_catalog,
        current_request=current_request,
        current_message=current_message,
    )
    issues.extend(proposal_issues)
    if proposal is not None and not operations and proposal.steps:
        try:
            operations = operations_from_proposal(proposal, capability_catalog)
        except (TypeError, ValueError) as error:
            issues.append(f"candidate capability is invalid: {str(error)[:512]}")

    output_spec = _output_from_patch(output_patch, base=base_output, operations=operations)
    if (
        current_request is not None
        and output_patch is None
        and any(change.field == "operations" for change in accepted_changes)
    ):
        output_spec = _default_output_spec(operations)

    # The request may be intentionally incomplete.  A placeholder request is
    # still a valid immutable domain object; P7PlanValidator decides whether
    # it needs clarification or is unsupported.
    request = CalculationRequest.create(
        request_id=f"draft_{turn_reference}",
        source_turn_id=turn_reference,
        molecule_kind=molecule_kind,
        molecule_value=molecule_value,
        charge=charge,
        multiplicity=multiplicity,
        operations=operations,
        method=method,
        environment=environment,
        hard_constraints=hard_constraints,
        prohibited_requests=prohibited_requests,
        output_spec=output_spec,
        preview_only=not intent.requested_execution,
    )
    if current_request is None:
        changed = True
    else:
        current_projection = current_request.model_dump(
            mode="json", exclude={"request_id", "source_turn_id", "request_hash"}
        )
        new_projection = request.model_dump(
            mode="json", exclude={"request_id", "source_turn_id", "request_hash"}
        )
        # A revision is not only a request-field merge.  The candidate graph,
        # geometry source selector, goal, and output declarations are durable
        # planning semantics too.  Compare the immutable planning record so a
        # corrected input_ref still creates a revision even when the request
        # fields remain identical.
        current_candidate = getattr(current_planning_record, "candidate", None)
        current_candidate_payload = (
            current_candidate.model_dump(mode="json")
            if hasattr(current_candidate, "model_dump")
            else thaw_json(current_candidate)
        )
        proposed_candidate_payload = (
            proposal.model_dump(mode="json") if proposal is not None else None
        )
        candidate_changed = (
            proposed_candidate_payload is not None
            and current_candidate_payload is not None
            and proposed_candidate_payload != current_candidate_payload
        )
        # A parameter-only sparse patch with no explicit proposal keeps the
        # existing graph; the current-turn wording is not itself a graph edit.
        if intent.plan_proposal is None and not any(
            change.field in {"operations", "hard_constraint", "prohibited_request"}
            for change in accepted_changes
        ):
            candidate_changed = False
        changed = current_projection != new_projection or candidate_changed or unreliable_change
    return DraftResolution(
        request=request,
        precheck=precheck,
        proposal=proposal,
        changes=tuple(accepted_changes),
        issues=tuple(dict.fromkeys(issues)),
        identity_notes=tuple(dict.fromkeys(notes)),
        changed=changed,
        policy_snapshot=policy.public_snapshot(),
        raw_molecule_fragment=raw_molecule_fragment,
    )


__all__ = ["DraftChange", "DraftResolution", "resolve_draft"]
