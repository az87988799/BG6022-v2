"""Deterministic P7 request normalisation and plan validation."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace

from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import WorkflowRecordId, new_id
from orca_agent.domain.json_types import thaw_json
from orca_agent.domain.p7_conversation import (
    CalculationAction,
    CalculationIntent,
    MoleculeInputType,
    ParameterSource,
    ParameterValue,
)
from orca_agent.domain.p7_intake import CalculationIntentV4
from orca_agent.domain.p7_planning import PlanProposal
from orca_agent.domain.p7_task import (
    CalculationPlan,
    CalculationRequest,
    OutputKind,
    OutputQuantity,
    OutputSpec,
    PlanNode,
    PlanValidation,
    ValidationStatus,
)
from orca_agent.planning.p5_protocols import P5_DEFAULT_PROTOCOL
from orca_agent.planning.p7_catalog import (
    P7_BASELINE_CAPABILITY_ID,
    CapabilityCatalog,
)
from orca_agent.planning.p7_draft import resolve_draft
from orca_agent.planning.p7_parameter_policy import (
    OXYGEN_CANONICAL_SMILES,
    MoleculePrecheck,
    MoleculePrecheckResult,
    accepted_recommendation,
)
from orca_agent.planning.p7_plan_compiler import (
    P7PlanCompiler,
    PlanCompilation,
    operations_from_proposal,
    proposal_from_operations,
)

DEFAULT_OPERATIONS = ("opt", "freq", "sp")
SUPPORTED_METHOD_NAMES = {"r2scan-3c", "r²scan-3c", "r2scan3c", "baseline.r2scan3c.v1"}
SUPPORTED_OUTPUT_NAMES = {item.value for item in OutputKind}


@dataclass(frozen=True)
class NormalizedPlan:
    request: CalculationRequest
    validation: PlanValidation
    plan: CalculationPlan | None
    precheck: MoleculePrecheckResult | None
    proposal: PlanProposal | None = None
    compilation: PlanCompilation | None = None
    source_task_id: str | None = None
    external_opt_result_id: str | None = None
    draft_semantics_version: str = "legacy"
    draft_changes: tuple[dict[str, object], ...] = ()
    policy_snapshot: dict[str, object] | None = None
    identity_notes: tuple[str, ...] = ()
    raw_molecule_fragment: str | None = None
    changed: bool = True


def _parameter(
    *,
    name: str,
    value: object,
    source: ParameterSource,
    source_reference: str,
    source_fragment: str | None = None,
    hard_constraint: bool = False,
    structure_hash: str | None = None,
    rule_version: str | None = None,
    recommendation_accepted: bool = False,
) -> ParameterValue:
    return ParameterValue(
        name=name,
        value=value,
        value_type="integer"
        if isinstance(value, int) and not isinstance(value, bool)
        else "string",
        hard_constraint=hard_constraint,
        source=source,
        source_reference=source_reference,
        source_fragment=source_fragment,
        structure_hash=structure_hash,
        rule_version=rule_version,
        recommendation_accepted=recommendation_accepted,
    )


def output_spec_for_operations(operations: tuple[str, ...]) -> OutputSpec:
    """Return the smallest output contract for one approved execution scope."""

    quantities: list[OutputQuantity] = []
    if operations == ("opt",):
        quantities.append(OutputQuantity(kind=OutputKind.OPTIMIZED_GEOMETRY))
    elif operations == ("sp",):
        quantities.append(
            OutputQuantity(
                kind=OutputKind.INDEPENDENT_SP_ELECTRONIC_ENERGY,
                source_selector="sp",
            )
        )
    elif operations == ("freq",):
        quantities.extend(
            (
                OutputQuantity(kind=OutputKind.VIBRATIONAL_FREQUENCIES, source_selector="freq"),
                OutputQuantity(kind=OutputKind.LOCAL_MINIMUM_SUPPORT),
            )
        )
    elif operations == ("opt", "freq"):
        quantities.extend(
            (
                OutputQuantity(kind=OutputKind.OPT_FINAL_ELECTRONIC_ENERGY, required=True),
                OutputQuantity(kind=OutputKind.VIBRATIONAL_FREQUENCIES, source_selector="freq"),
                OutputQuantity(kind=OutputKind.LOCAL_MINIMUM_SUPPORT),
            )
        )
    elif operations == ("opt", "sp"):
        quantities.extend(
            (
                OutputQuantity(kind=OutputKind.OPTIMIZED_GEOMETRY),
                OutputQuantity(
                    kind=OutputKind.INDEPENDENT_SP_ELECTRONIC_ENERGY,
                    source_selector="sp",
                ),
            )
        )
    else:
        return OutputSpec.default()
    return OutputSpec.create(quantities=quantities)


def output_spec_from_intent(
    intent: CalculationIntent,
    *,
    operations: tuple[str, ...] | None = None,
    base_spec: OutputSpec | None = None,
) -> OutputSpec:
    """Parse the model's rendering proposal without accepting arbitrary kinds."""

    raw = thaw_json(intent.output_spec)
    if not isinstance(raw, dict) or not raw:
        if base_spec is not None:
            return base_spec
        return (
            OutputSpec.default() if operations is None else output_spec_for_operations(operations)
        )
    raw_quantities = raw.get("quantities")
    if raw_quantities is None:
        if "quantities" in raw:
            raise ValueError("output_spec.quantities must be a non-empty array")
        if base_spec is not None:
            raw_quantities = [item.model_dump(mode="python") for item in base_spec.quantities]
        elif operations is not None:
            raw_quantities = [
                item.model_dump(mode="python")
                for item in output_spec_for_operations(operations).quantities
            ]
        else:
            # Formatting-only model output (layout/style/language) does not
            # change the scientific output scope.  Preserve the historical
            # default for callers that have no base request yet.
            return OutputSpec.default()
    if not isinstance(raw_quantities, list) or not raw_quantities:
        raise ValueError("output_spec.quantities must be a non-empty array")
    quantities: list[OutputQuantity] = []
    for item in raw_quantities:
        if not isinstance(item, dict):
            raise ValueError("output quantity must be an object")
        kind = item.get("kind")
        if kind not in SUPPORTED_OUTPUT_NAMES:
            raise ValueError(f"unsupported output kind: {kind}")
        quantities.append(
            OutputQuantity(
                kind=OutputKind(kind),
                required=item.get("required", True),
                source_selector=item.get("source_selector"),
                unit=item.get("unit"),
                label=item.get("label"),
            )
        )
    units = raw.get("units", {})
    if not isinstance(units, dict):
        raise ValueError("output_spec.units must be an object")
    artifacts = raw.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise ValueError("output_spec.artifacts must be an array")
    return OutputSpec.create(
        quantities=quantities,
        language=raw.get("language", "zh" if base_spec is None else base_spec.language),
        style=raw.get("style", "concise" if base_spec is None else base_spec.style),
        layout=raw.get("layout", "prose" if base_spec is None else base_spec.layout),
        units=units
        if "units" in raw
        else ({} if base_spec is None else thaw_json(base_spec.units)),
        precision=raw.get("precision", None if base_spec is None else base_spec.precision),
        artifacts=artifacts if "artifacts" in raw else base_spec.artifacts if base_spec else (),
        include_evidence=raw.get(
            "include_evidence", False if base_spec is None else base_spec.include_evidence
        ),
    )


class P7PlanValidator:
    """Validate all deterministic boundaries before any P4/P5 call."""

    def __init__(
        self,
        catalog: CapabilityCatalog,
        *,
        precheck: MoleculePrecheck | None = None,
        enable_candidate_planning: bool = True,
    ) -> None:
        self.catalog = catalog
        self.precheck = precheck or MoleculePrecheck()
        self.enable_candidate_planning = enable_candidate_planning
        self.compiler = P7PlanCompiler(catalog)

    def normalize(
        self,
        intent: CalculationIntent | CalculationIntentV4,
        *,
        turn_id: str,
        existing_request: CalculationRequest | None = None,
        source_request: CalculationRequest | None = None,
        accept_recommendations: bool = False,
        user_text: str | None = None,
        trusted_artifacts: Mapping[str, Mapping[str, object]] | None = None,
        current_planning_record: object | None = None,
    ) -> NormalizedPlan:
        if isinstance(intent, CalculationIntentV4):
            return self._normalize_v4(
                intent,
                turn_id=turn_id,
                existing_request=existing_request,
                source_request=source_request,
                user_text=user_text or "",
                trusted_artifacts=trusted_artifacts,
                current_planning_record=current_planning_record,
            )
        proposal, proposal_error = self._proposal_from_intent(intent)
        if existing_request is not None and intent.action is CalculationAction.REVISE_DRAFT:
            values = existing_request.model_dump(mode="python")
            # Pydantic's model_dump returns dictionaries for nested models;
            # restore the strict domain objects before CalculationRequest.create
            # so recommendation provenance remains actionable on revision.
            for field_name in ("charge", "multiplicity", "method", "environment", "output_spec"):
                values[field_name] = getattr(existing_request, field_name)
            if intent.molecule_kind is not None:
                molecule_changed = (
                    intent.molecule_kind != existing_request.molecule_kind
                    or intent.molecule_value != existing_request.molecule_value
                )
                values["molecule_kind"] = intent.molecule_kind
                values["molecule_value"] = intent.molecule_value
                if molecule_changed:
                    # Structure-derived q/M recommendations belong to the old
                    # identity and must never survive a molecule change.
                    values["charge"] = None
                    values["multiplicity"] = None
            if intent.charge is not None:
                charge_quote = _explicit_evidence(intent, "charge", user_text)
                values["charge"] = _parameter(
                    name="charge",
                    value=intent.charge,
                    source=(
                        ParameterSource.USER_EXPLICIT
                        if charge_quote is not None
                        else ParameterSource.UNRESOLVED
                    ),
                    source_reference=turn_id,
                    source_fragment=charge_quote,
                    hard_constraint=True,
                    structure_hash=getattr(existing_request.charge, "structure_hash", None),
                )
            if intent.multiplicity is not None:
                multiplicity_quote = _explicit_evidence(intent, "multiplicity", user_text)
                values["multiplicity"] = _parameter(
                    name="multiplicity",
                    value=intent.multiplicity,
                    source=(
                        ParameterSource.USER_EXPLICIT
                        if multiplicity_quote is not None
                        else ParameterSource.UNRESOLVED
                    ),
                    source_reference=turn_id,
                    source_fragment=multiplicity_quote,
                    hard_constraint=True,
                    structure_hash=getattr(existing_request.multiplicity, "structure_hash", None),
                )
            if intent.operations:
                values["operations"] = intent.operations
            elif self.enable_candidate_planning and proposal is not None:
                values["operations"] = operations_from_proposal(proposal, self.catalog)
            if intent.method is not None:
                method_quote = _explicit_evidence(intent, "method", user_text)
                values["method"] = _parameter(
                    name="method",
                    value=intent.method,
                    source=(
                        ParameterSource.USER_EXPLICIT
                        if method_quote is not None
                        else ParameterSource.UNRESOLVED
                    ),
                    source_reference=turn_id,
                    source_fragment=method_quote,
                    hard_constraint=True,
                )
            if intent.environment is not None:
                environment_quote = _explicit_evidence(intent, "environment", user_text)
                values["environment"] = _parameter(
                    name="environment",
                    value=intent.environment,
                    source=(
                        ParameterSource.USER_EXPLICIT
                        if environment_quote is not None
                        else ParameterSource.UNRESOLVED
                    ),
                    source_reference=turn_id,
                    source_fragment=environment_quote,
                    hard_constraint=True,
                )
            if accept_recommendations:
                for field_name in ("charge", "multiplicity"):
                    current = values.get(field_name)
                    if (
                        isinstance(current, ParameterValue)
                        and current.source is ParameterSource.POLICY_RECOMMENDED
                    ):
                        values[field_name] = accepted_recommendation(current)
            try:
                values["output_spec"] = (
                    output_spec_from_intent(
                        intent,
                        operations=tuple(values["operations"]),
                        base_spec=existing_request.output_spec,
                    )
                    if thaw_json(intent.output_spec)
                    else existing_request.output_spec
                )
            except ValueError as error:
                values["output_spec"] = existing_request.output_spec
                values["prohibited_requests"] = tuple(
                    (
                        *existing_request.prohibited_requests,
                        f"invalid output specification: {error}",
                    )
                )
            values["source_turn_id"] = turn_id
            values["request_id"] = str(new_id(WorkflowRecordId))
            values["preview_only"] = not intent.requested_execution
            request = CalculationRequest.create(**values)
        else:
            request = self._new_request(
                intent,
                turn_id=turn_id,
                accept_recommendations=accept_recommendations,
                user_text=user_text,
                proposal=proposal,
                source_request=source_request,
            )

        normalized = self.validate(
            request,
            intent=intent,
            proposal=proposal,
            proposal_error=proposal_error,
            trusted_artifacts=trusted_artifacts,
        )
        return normalized

    def _normalize_v4(
        self,
        intent: CalculationIntentV4,
        *,
        turn_id: str,
        existing_request: CalculationRequest | None,
        source_request: CalculationRequest | None,
        user_text: str,
        trusted_artifacts: Mapping[str, Mapping[str, object]] | None,
        current_planning_record: object | None,
    ) -> NormalizedPlan:
        resolution = resolve_draft(
            current_request=existing_request or source_request,
            current_planning_record=current_planning_record,
            user_patch=intent,
            current_message=user_text,
            turn_reference=turn_id,
            capability_catalog=self.catalog,
        )
        proposal_error = next(
            (item for item in resolution.issues if item.startswith("invalid candidate plan:")),
            None,
        )
        normalized = self.validate(
            resolution.request,
            intent=intent,
            proposal=resolution.proposal,
            proposal_error=proposal_error,
            trusted_artifacts=trusted_artifacts,
            allow_policy_recommendations=True,
            resolution_issues=resolution.issues,
        )
        return replace(
            normalized,
            draft_semantics_version="p7.draft.v4",
            draft_changes=tuple(change.as_dict() for change in resolution.changes),
            policy_snapshot=resolution.policy_snapshot,
            identity_notes=resolution.identity_notes,
            raw_molecule_fragment=resolution.raw_molecule_fragment,
            changed=resolution.changed,
        )

    @staticmethod
    def _proposal_from_intent(
        intent: CalculationIntent,
    ) -> tuple[PlanProposal | None, str | None]:
        if intent.plan_proposal is None:
            return None, None
        try:
            return (
                PlanProposal.model_validate_json(
                    json.dumps(thaw_json(intent.plan_proposal), ensure_ascii=False), strict=True
                ),
                None,
            )
        except (TypeError, ValueError) as error:
            return None, str(error)[:512]

    def _new_request(
        self,
        intent: CalculationIntent,
        *,
        turn_id: str,
        accept_recommendations: bool,
        user_text: str | None,
        proposal: PlanProposal | None,
        source_request: CalculationRequest | None = None,
    ) -> CalculationRequest:
        if source_request is not None:
            return self._new_follow_up_request(
                intent,
                turn_id=turn_id,
                proposal=proposal,
                source_request=source_request,
            )
        source_reference = turn_id
        precheck: MoleculePrecheckResult | None = None
        if intent.molecule_kind is MoleculeInputType.SMILES and intent.molecule_value is not None:
            precheck = self.precheck.check(
                input_kind=intent.molecule_kind,
                raw_input=intent.molecule_value,
                charge=intent.charge,
            )
        charge: ParameterValue | None = None
        multiplicity: ParameterValue | None = None
        if precheck is not None and precheck.status == "valid":
            charge_quote = _explicit_evidence(intent, "charge", user_text)
            multiplicity_quote = _explicit_evidence(intent, "multiplicity", user_text)
            if intent.charge is not None and charge_quote is None:
                charge = _parameter(
                    name="charge",
                    value=intent.charge,
                    source=ParameterSource.UNRESOLVED,
                    source_reference=source_reference,
                    source_fragment=None,
                    hard_constraint=True,
                    structure_hash=precheck.structure_hash,
                )
            if intent.multiplicity is not None and multiplicity_quote is None:
                multiplicity = _parameter(
                    name="multiplicity",
                    value=intent.multiplicity,
                    source=ParameterSource.UNRESOLVED,
                    source_reference=source_reference,
                    source_fragment=None,
                    hard_constraint=True,
                    structure_hash=precheck.structure_hash,
                )
            charge_resolution = self.precheck.resolve_electronic_state(
                precheck=precheck,
                requested_charge=intent.charge if charge_quote is not None else None,
                requested_multiplicity=(
                    intent.multiplicity if multiplicity_quote is not None else None
                ),
                source_reference=source_reference,
                charge_source_fragment=charge_quote,
                multiplicity_source_fragment=multiplicity_quote,
            )
            if charge is None:
                charge = charge_resolution.charge
            if multiplicity is None:
                multiplicity = charge_resolution.multiplicity
            if precheck.canonical_isomeric_smiles == OXYGEN_CANONICAL_SMILES:
                intent = intent.model_copy(
                    update={
                        "prohibited_requests": tuple(
                            (*intent.prohibited_requests, *charge_resolution.issues)
                        )
                    }
                )
            if (
                accept_recommendations
                and multiplicity is not None
                and multiplicity.source is ParameterSource.POLICY_RECOMMENDED
            ):
                multiplicity = accepted_recommendation(multiplicity)
        elif intent.molecule_kind in {
            MoleculeInputType.NAME,
            MoleculeInputType.CAS,
            MoleculeInputType.CID,
        }:
            known = (
                intent.molecule_value.casefold()
                in {
                    "water",
                    "ethanol",
                    "64-17-5",
                    "7732-18-5",
                    "702",
                    "962",
                }
                if intent.molecule_value is not None
                else False
            )
            if intent.charge is not None:
                charge = _parameter(
                    name="charge",
                    value=intent.charge,
                    source=(
                        ParameterSource.USER_EXPLICIT
                        if _explicit_evidence(intent, "charge", user_text) is not None
                        else ParameterSource.UNRESOLVED
                    ),
                    source_reference=source_reference,
                    source_fragment=_explicit_evidence(intent, "charge", user_text),
                    hard_constraint=True,
                )
            elif known:
                charge = _parameter(
                    name="charge",
                    value=0,
                    source=ParameterSource.POLICY_RECOMMENDED,
                    source_reference=source_reference,
                    rule_version="p7-parameter-policy-v1",
                )
            if intent.multiplicity is not None:
                multiplicity = _parameter(
                    name="multiplicity",
                    value=intent.multiplicity,
                    source=(
                        ParameterSource.USER_EXPLICIT
                        if _explicit_evidence(intent, "multiplicity", user_text) is not None
                        else ParameterSource.UNRESOLVED
                    ),
                    source_reference=source_reference,
                    source_fragment=_explicit_evidence(intent, "multiplicity", user_text),
                    hard_constraint=True,
                )
            elif known:
                multiplicity = _parameter(
                    name="multiplicity",
                    value=1,
                    source=ParameterSource.POLICY_RECOMMENDED,
                    source_reference=source_reference,
                    rule_version="p7-parameter-policy-v1",
                )
        else:
            if intent.charge is not None:
                charge = _parameter(
                    name="charge",
                    value=intent.charge,
                    source=(
                        ParameterSource.USER_EXPLICIT
                        if _explicit_evidence(intent, "charge", user_text) is not None
                        else ParameterSource.UNRESOLVED
                    ),
                    source_reference=source_reference,
                    source_fragment=_explicit_evidence(intent, "charge", user_text),
                    hard_constraint=True,
                )
            if intent.multiplicity is not None:
                multiplicity = _parameter(
                    name="multiplicity",
                    value=intent.multiplicity,
                    source=(
                        ParameterSource.USER_EXPLICIT
                        if _explicit_evidence(intent, "multiplicity", user_text) is not None
                        else ParameterSource.UNRESOLVED
                    ),
                    source_reference=source_reference,
                    source_fragment=_explicit_evidence(intent, "multiplicity", user_text),
                    hard_constraint=True,
                )

        if accept_recommendations:
            if charge is not None and charge.source is ParameterSource.POLICY_RECOMMENDED:
                charge = accepted_recommendation(charge)
            if (
                multiplicity is not None
                and multiplicity.source is ParameterSource.POLICY_RECOMMENDED
            ):
                multiplicity = accepted_recommendation(multiplicity)

        method_quote = _explicit_evidence(intent, "method", user_text)
        environment_quote = _explicit_evidence(intent, "environment", user_text)
        method_value = intent.method or "r2SCAN-3c"
        environment_value = intent.environment or "gas"
        method = _parameter(
            name="method",
            value=method_value,
            source=(
                ParameterSource.USER_EXPLICIT
                if intent.method and method_quote is not None
                else ParameterSource.UNRESOLVED
                if intent.method
                else ParameterSource.POLICY_RECOMMENDED
            ),
            source_reference=source_reference,
            source_fragment=method_quote,
            hard_constraint=intent.method is not None,
            rule_version=None if intent.method else "p7-parameter-policy-v1",
        )
        environment = _parameter(
            name="environment",
            value=environment_value,
            source=(
                ParameterSource.USER_EXPLICIT
                if intent.environment and environment_quote is not None
                else ParameterSource.UNRESOLVED
                if intent.environment
                else ParameterSource.POLICY_RECOMMENDED
            ),
            source_reference=source_reference,
            source_fragment=environment_quote,
            hard_constraint=intent.environment is not None,
            rule_version=None if intent.environment else "p7-parameter-policy-v1",
        )
        output_error: str | None = None
        try:
            operations = tuple(intent.operations)
            if self.enable_candidate_planning and not operations and proposal is not None:
                operations = operations_from_proposal(proposal, self.catalog)
            output_spec = output_spec_from_intent(
                intent,
                operations=operations if self.enable_candidate_planning else None,
            )
        except (TypeError, ValueError) as error:
            output_spec = OutputSpec.default()
            output_error = str(error)
        prohibited_requests = tuple(intent.prohibited_requests)
        if output_error is not None:
            prohibited_requests = (
                *prohibited_requests,
                f"invalid output specification: {output_error}",
            )
        return CalculationRequest.create(
            request_id=str(new_id(WorkflowRecordId)),
            source_turn_id=turn_id,
            molecule_kind=intent.molecule_kind,
            molecule_value=intent.molecule_value,
            charge=charge,
            multiplicity=multiplicity,
            operations=(
                tuple(intent.operations)
                or (
                    operations_from_proposal(proposal, self.catalog)
                    if self.enable_candidate_planning and proposal is not None
                    else ()
                )
                or (() if self.enable_candidate_planning else DEFAULT_OPERATIONS)
            ),
            method=method,
            environment=environment,
            hard_constraints=intent.hard_constraints,
            prohibited_requests=prohibited_requests,
            output_spec=output_spec,
            preview_only=not intent.requested_execution,
        )

    def _new_follow_up_request(
        self,
        intent: CalculationIntent,
        *,
        turn_id: str,
        proposal: PlanProposal | None,
        source_request: CalculationRequest,
    ) -> CalculationRequest:
        """Create a new scope from a trusted task without re-resolving identity.

        This path is intentionally separate from draft revision.  A completed
        Opt task is immutable; a Freq follow-up receives a new request/task
        while retaining the exact P4 identity and parameter provenance needed
        by the P5 external-result binding.
        """

        operations = tuple(intent.operations)
        if self.enable_candidate_planning and not operations and proposal is not None:
            operations = operations_from_proposal(proposal, self.catalog)
        if self.enable_candidate_planning and not operations:
            operations = ("sp",)
        output_spec = output_spec_from_intent(
            intent,
            operations=operations if self.enable_candidate_planning else None,
        )
        values = source_request.model_dump(mode="python")
        for field_name in ("charge", "multiplicity", "method", "environment", "output_spec"):
            values[field_name] = getattr(source_request, field_name)
        values.update(
            {
                "request_id": str(new_id(WorkflowRecordId)),
                "source_turn_id": turn_id,
                "operations": operations,
                "output_spec": output_spec,
                "preview_only": not intent.requested_execution,
                "hard_constraints": tuple(
                    dict.fromkeys((*source_request.hard_constraints, *intent.hard_constraints))
                ),
                "prohibited_requests": tuple(
                    dict.fromkeys(
                        (*source_request.prohibited_requests, *intent.prohibited_requests)
                    )
                ),
            }
        )
        return CalculationRequest.create(**values)

    def validate(
        self,
        request: CalculationRequest,
        *,
        intent: CalculationIntent | CalculationIntentV4 | None = None,
        proposal: PlanProposal | None = None,
        proposal_error: str | None = None,
        trusted_artifacts: Mapping[str, Mapping[str, object]] | None = None,
        allow_policy_recommendations: bool = False,
        resolution_issues: Iterable[str] = (),
    ) -> NormalizedPlan:
        issues: list[str] = list(resolution_issues)
        missing: list[str] = []
        unsupported: list[str] = list(request.prohibited_requests)
        precheck = None
        if request.molecule_kind is None or request.molecule_value is None:
            missing.append("molecule")
        elif request.molecule_kind is MoleculeInputType.SMILES:
            precheck = self.precheck.check(
                input_kind=request.molecule_kind,
                raw_input=request.molecule_value,
                charge=None if request.charge is None else int(request.charge.value),
            )
            if precheck.status != "valid":
                issues.append(precheck.reason_code or "invalid_structure")
        if request.method is None:
            missing.append("method")
        else:
            if request.method.source is ParameterSource.UNRESOLVED:
                missing.append("method_evidence")
            method = str(request.method.value).casefold()
            if method not in SUPPORTED_METHOD_NAMES:
                unsupported.append(f"method:{request.method.value}")
        if request.environment is None:
            missing.append("environment")
        elif str(request.environment.value).casefold() not in {"gas", "gas-phase", "气相"}:
            unsupported.append(f"environment:{request.environment.value}")
        elif request.environment.source is ParameterSource.UNRESOLVED:
            missing.append("environment_evidence")

        operations = tuple(request.operations)
        if not self.enable_candidate_planning and operations != DEFAULT_OPERATIONS:
            unsupported.append("only the complete Opt→Freq→independent SP protocol is available")
        if any(
            marker.casefold()
            in {
                "gibbs",
                "gibbs_free_energy",
                "free_energy",
                "zpe",
                "solvent",
                "ts",
                "irc",
                "global_minimum",
            }
            for marker in (*request.prohibited_requests, *request.hard_constraints)
        ):
            unsupported.append("requested result or workflow is outside the P7 capability")

        if request.charge is None:
            missing.append("charge")
        elif request.charge.source is ParameterSource.UNRESOLVED:
            missing.append("charge_evidence")
        if request.multiplicity is None:
            missing.append("multiplicity")
        elif request.multiplicity.source is ParameterSource.UNRESOLVED:
            missing.append("multiplicity_evidence")
        if (
            request.multiplicity is not None
            and request.multiplicity.source is ParameterSource.POLICY_RECOMMENDED
            and not allow_policy_recommendations
        ):
            missing.append("accept_electronic_state_recommendation")
        if request.charge is not None and int(request.charge.value) != 0:
            unsupported.append("non-neutral charge")
        if request.multiplicity is not None and int(request.multiplicity.value) != 1:
            unsupported.append("non-singlet electronic state")

        try:
            # Re-parse the strict OutputSpec from the request, ensuring that a
            # malformed model proposal cannot become a plan by accident.
            request.output_spec.model_dump(mode="json")
        except (TypeError, ValueError):
            unsupported.append("invalid output specification")

        if self.enable_candidate_planning and proposal_error is not None:
            issues.append(f"invalid candidate plan: {proposal_error}")

        # A v4 request may intentionally contain a contradictory pair of
        # requirements: frequency is prohibited while local-minimum support
        # is still requested.  Keep this as an editable clarification so the
        # compiler can surface the choice; do not classify the conflict as a
        # generic unsupported workflow before it reaches that boundary.
        local_minimum_conflict = (
            isinstance(intent, CalculationIntentV4)
            and "frequency is prohibited while local-minimum support is requested"
            in unsupported
            and any(
                quantity.kind is OutputKind.LOCAL_MINIMUM_SUPPORT
                for quantity in request.output_spec.quantities
            )
        )
        if local_minimum_conflict:
            unsupported = [
                item
                for item in unsupported
                if item != "frequency is prohibited while local-minimum support is requested"
            ]

        if unsupported:
            validation = PlanValidation.create(
                status=ValidationStatus.UNSUPPORTED,
                valid=False,
                issues=tuple(dict.fromkeys(issues)),
                missing_fields=tuple(dict.fromkeys(missing)),
                unsupported_requests=tuple(dict.fromkeys(unsupported)),
                request_hash=request.request_hash,
            )
            return NormalizedPlan(request, validation, None, precheck, proposal=proposal)
        if missing:
            question = self._clarification_question(missing, request)
            validation = PlanValidation.create(
                status=ValidationStatus.NEEDS_CLARIFICATION,
                valid=False,
                issues=tuple(dict.fromkeys(issues)),
                missing_fields=tuple(dict.fromkeys(missing)),
                clarification_question=question,
                request_hash=request.request_hash,
            )
            return NormalizedPlan(request, validation, None, precheck, proposal=proposal)
        if issues:
            validation = PlanValidation.create(
                status=ValidationStatus.INVALID,
                valid=False,
                issues=tuple(dict.fromkeys(issues)),
                request_hash=request.request_hash,
            )
            return NormalizedPlan(request, validation, None, precheck, proposal=proposal)

        if self.enable_candidate_planning:
            candidate = proposal or proposal_from_operations(
                operations,
                goal=(
                    "、".join(item.kind.value for item in request.output_spec.quantities)
                    or "完成受支持的化学计算"
                ),
                requested_outputs=tuple(item.kind.value for item in request.output_spec.quantities),
                action=(
                    intent.action.value
                    if intent is not None and hasattr(intent.action, "value")
                    else str(intent.action)
                    if intent is not None
                    else "plan_new"
                ),
            )
            if proposal is None and not operations:
                candidate = candidate.model_copy(
                    update={"clarification_fields": ("execution_scope",)}
                )
            validation = PlanValidation.create(
                status=ValidationStatus.VALID,
                valid=True,
                issues=(),
                request_hash=request.request_hash,
            )
            compilation = self.compiler.compile(
                request,
                candidate,
                validation=validation,
                trusted_artifacts=trusted_artifacts,
            )
            if compilation.status is not ValidationStatus.VALID or compilation.plan is None:
                missing_fields = (
                    ("trusted_opt_result",)
                    if compilation.status is ValidationStatus.NEEDS_CLARIFICATION
                    and any(
                        "trusted Opt" in item or "frequency requires" in item
                        for item in compilation.issues
                    )
                    else ()
                )
                validation = PlanValidation.create(
                    status=compilation.status,
                    valid=False,
                    issues=tuple(dict.fromkeys(compilation.issues)),
                    missing_fields=missing_fields,
                    unsupported_requests=(
                        tuple(dict.fromkeys(compilation.issues))
                        if compilation.status is ValidationStatus.UNSUPPORTED
                        else ()
                    ),
                    clarification_question=compilation.clarification_question,
                    request_hash=request.request_hash,
                )
                return NormalizedPlan(
                    request,
                    validation,
                    None,
                    precheck,
                    proposal=candidate,
                    compilation=compilation,
                    source_task_id=compilation.source_task_id,
                    external_opt_result_id=compilation.external_opt_result_id,
                )
            return NormalizedPlan(
                request,
                validation,
                compilation.plan,
                precheck,
                proposal=candidate,
                compilation=compilation,
                source_task_id=compilation.source_task_id,
                external_opt_result_id=compilation.external_opt_result_id,
            )

        entry = self.catalog.require(P7_BASELINE_CAPABILITY_ID, "1")
        protocol = P5_DEFAULT_PROTOCOL
        nodes = tuple(
            PlanNode(
                node_id=f"{protocol.protocol_id}:node-{index + 1}",
                kind=node_kind.value,
                depends_on=tuple(f"{protocol.protocol_id}:node-{dep + 1}" for dep in dependencies),
                geometry_source=source.value,
                method_profile_id=entry.descriptor.method_profile_id,
                budget=protocol.budget_for(node_kind).model_dump(mode="json"),
            )
            for index, (node_kind, source, dependencies) in enumerate(
                zip(protocol.nodes, protocol.geometry_sources, protocol.dependencies, strict=True)
            )
        )
        registry_id = entry.descriptor.registry_snapshot_id
        registry_hash = entry.descriptor.registry_snapshot_hash
        expected = tuple(dict.fromkeys(item.kind for item in request.output_spec.quantities))
        plan = CalculationPlan.create(
            capability_id=entry.descriptor.capability_id,
            capability_version=entry.descriptor.version,
            capability_hash=entry.descriptor.content_hash,
            request_hash=request.request_hash,
            validation_hash="0" * 64,
            output_spec_hash=request.output_spec.output_spec_hash,
            method_profile_id=entry.descriptor.method_profile_id,
            method_profile_hash=entry.descriptor.method_profile_hash,
            environment="gas",
            protocol_id=protocol.protocol_id,
            protocol_hash=protocol.protocol_hash,
            registry_snapshot_id=registry_id,
            registry_snapshot_hash=registry_hash,
            nodes=nodes,
            expected_outputs=expected,
            applicability={
                "input_kinds": list(entry.descriptor.input_kinds),
                "neutral_closed_shell_singlet": True,
                "single_component": True,
                "one_starting_conformer": True,
            },
            resources={
                "nprocs": protocol.nprocs,
                "total_memory_mb": protocol.total_memory_mb,
                "maxcore_mb": protocol.maxcore_mb,
                "implicit_threads": protocol.implicit_threads,
                "wall_time_seconds": dict(protocol.wall_time_seconds),
            },
            scope_note=(
                "one starting conformer; local minimum support is not a global minimum claim"
            ),
        )
        validation = PlanValidation.create(
            status=ValidationStatus.VALID,
            valid=True,
            issues=(),
            request_hash=request.request_hash,
        )
        # The validation hash is a plan input.  Rebuild the plan once with the
        # actual validation record rather than allowing a placeholder hash.
        plan = plan.model_copy(
            update={
                "validation_hash": validation.validation_hash,
                "plan_hash": "0" * 64,
            }
        )
        plan = plan.model_copy(
            update={"plan_hash": sha256_hex(plan.model_dump(mode="json", exclude={"plan_hash"}))}
        )
        return NormalizedPlan(request, validation, plan, precheck, proposal=proposal)

    @staticmethod
    def _clarification_question(missing: Iterable[str], request: CalculationRequest) -> str:
        fields = tuple(missing)
        if "accept_electronic_state_recommendation" in fields:
            return "当前结构匹配中性闭壳层单重态推荐（电荷 0、多重度 1）。是否接受该推荐？"
        labels = {
            "molecule": "具体分子（name/CAS/CID/SMILES）",
            "charge": "电荷",
            "multiplicity": "多重度",
            "method": "方法",
            "environment": "环境",
            "method_evidence": "方法的原文依据",
            "environment_evidence": "环境的原文依据",
            "charge_evidence": "电荷的原文依据",
            "multiplicity_evidence": "多重度的原文依据",
        }
        return "请补充：" + "、".join(labels.get(item, item) for item in fields[:3]) + "。"


def _explicit_evidence(
    intent: CalculationIntent, field_name: str, user_text: str | None
) -> str | None:
    """Return evidence only when it quotes the value positively in this turn.

    Presence of a quote is not enough: a model can quote a prohibition or put a
    different value next to a field.  This small semantic gate keeps those
    proposals unresolved so the deterministic validator can ask the user.
    """

    if not user_text:
        return None
    evidence = thaw_json(intent.parameter_evidence)
    if not isinstance(evidence, dict):
        return None
    raw = evidence.get(field_name)
    if not isinstance(raw, dict):
        return None
    origin = raw.get("origin", raw.get("source"))
    quote = raw.get("quote", raw.get("source_fragment"))
    if origin not in {"user", "user_explicit", "current_message"}:
        return None
    if not isinstance(quote, str) or not quote.strip():
        return None
    quote = quote.strip()
    text_folded = user_text.casefold()
    quote_start = text_folded.find(quote.casefold())
    if quote_start < 0:
        return None
    value = getattr(intent, field_name, None)
    pattern = _evidence_value_pattern(field_name, value)
    if pattern is None:
        return None
    match = pattern.search(quote)
    if match is None:
        return None
    absolute_end = quote_start + match.end()
    semantic_window = user_text[max(0, absolute_end - 48) : absolute_end]
    if _NEGATIVE_EVIDENCE.search(semantic_window):
        return None
    return quote


_NEGATIVE_EVIDENCE = re.compile(
    r"(?:不要|不使用|不采用|禁止|排除|不必|无需|不需要|不是|不选|不要求|"
    r"without|do\s+not|don't|not|no)\s*(?:使用|采用|指定|选择|要求|use|using)?"
    r"[\s\S]{0,24}$",
    re.IGNORECASE,
)


def _evidence_value_pattern(field_name: str, value: object) -> re.Pattern[str] | None:
    if value is None:
        return None
    if field_name in {"charge", "multiplicity"} and isinstance(value, int):
        return re.compile(rf"(?<!\d)[+-]?{abs(value)}(?!\d)")
    if field_name == "method":
        normalized = _method_evidence_key(str(value))
        if normalized == "r2scan3c":
            return re.compile(r"r\s*[²2]?\s*scan\s*[- ]?\s*3c|baseline\.r2scan3c\.v1", re.I)
        return re.compile(re.escape(str(value)), re.I)
    if field_name == "environment":
        normalized = str(value).casefold().strip()
        if normalized in {"gas", "gas-phase", "gas phase", "气相"}:
            return re.compile(r"gas\s*[- ]?phase|gas\b|气相", re.I)
        return re.compile(re.escape(str(value)), re.I)
    return None


def _method_evidence_key(value: str) -> str:
    folded = value.casefold().replace("²", "2")
    compact = re.sub(r"[^a-z0-9]+", "", folded)
    return "r2scan3c" if "r2scan3c" in compact else compact


__all__ = [
    "DEFAULT_OPERATIONS",
    "NormalizedPlan",
    "P7PlanValidator",
    "output_spec_for_operations",
    "output_spec_from_intent",
]
