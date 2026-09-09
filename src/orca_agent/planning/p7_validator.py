"""Deterministic P7 request normalisation and plan validation."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

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
from orca_agent.planning.p7_parameter_policy import (
    OXYGEN_CANONICAL_SMILES,
    MoleculePrecheck,
    MoleculePrecheckResult,
    accepted_recommendation,
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


def output_spec_from_intent(intent: CalculationIntent) -> OutputSpec:
    """Parse the model's rendering proposal without accepting arbitrary kinds."""

    raw = thaw_json(intent.output_spec)
    if not isinstance(raw, dict) or not raw:
        return OutputSpec.default()
    raw_quantities = raw.get("quantities")
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
        language=raw.get("language", "zh"),
        style=raw.get("style", "concise"),
        layout=raw.get("layout", "prose"),
        units=units,
        precision=raw.get("precision"),
        artifacts=artifacts,
        include_evidence=raw.get("include_evidence", False),
    )


class P7PlanValidator:
    """Validate all deterministic boundaries before any P4/P5 call."""

    def __init__(
        self,
        catalog: CapabilityCatalog,
        *,
        precheck: MoleculePrecheck | None = None,
    ) -> None:
        self.catalog = catalog
        self.precheck = precheck or MoleculePrecheck()

    def normalize(
        self,
        intent: CalculationIntent,
        *,
        turn_id: str,
        existing_request: CalculationRequest | None = None,
        accept_recommendations: bool = False,
        user_text: str | None = None,
    ) -> NormalizedPlan:
        if existing_request is not None and intent.action is CalculationAction.REVISE_DRAFT:
            values = existing_request.model_dump(mode="python")
            # Pydantic's model_dump returns dictionaries for nested models;
            # restore the strict domain objects before CalculationRequest.create
            # so recommendation provenance remains actionable on revision.
            for field_name in ("charge", "multiplicity", "method", "environment", "output_spec"):
                values[field_name] = getattr(existing_request, field_name)
            if intent.molecule_kind is not None:
                values["molecule_kind"] = intent.molecule_kind
                values["molecule_value"] = intent.molecule_value
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
                values["output_spec"] = output_spec_from_intent(intent)
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
            )

        return self.validate(request, intent=intent)

    def _new_request(
        self,
        intent: CalculationIntent,
        *,
        turn_id: str,
        accept_recommendations: bool,
        user_text: str | None,
    ) -> CalculationRequest:
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
            output_spec = output_spec_from_intent(intent)
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
            operations=intent.operations or DEFAULT_OPERATIONS,
            method=method,
            environment=environment,
            hard_constraints=intent.hard_constraints,
            prohibited_requests=prohibited_requests,
            output_spec=output_spec,
            preview_only=not intent.requested_execution,
        )

    def validate(
        self,
        request: CalculationRequest,
        *,
        intent: CalculationIntent | None = None,
    ) -> NormalizedPlan:
        issues: list[str] = []
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
        if operations != DEFAULT_OPERATIONS:
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

        if unsupported:
            validation = PlanValidation.create(
                status=ValidationStatus.UNSUPPORTED,
                valid=False,
                issues=tuple(dict.fromkeys(issues)),
                missing_fields=tuple(dict.fromkeys(missing)),
                unsupported_requests=tuple(dict.fromkeys(unsupported)),
                request_hash=request.request_hash,
            )
            return NormalizedPlan(request, validation, None, precheck)
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
            return NormalizedPlan(request, validation, None, precheck)
        if issues:
            validation = PlanValidation.create(
                status=ValidationStatus.INVALID,
                valid=False,
                issues=tuple(dict.fromkeys(issues)),
                request_hash=request.request_hash,
            )
            return NormalizedPlan(request, validation, None, precheck)

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
        return NormalizedPlan(request, validation, plan, precheck)

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
    "output_spec_from_intent",
]
