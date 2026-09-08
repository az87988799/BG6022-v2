"""Read-only P5 source closure, parser replay, and P6 evidence construction.

This module is deliberately downstream of P5.  It never starts a calculation,
changes a P5 record, or asks an external service for missing information.  A
source result is accepted only when its complete result -> action -> binding ->
execution/job -> artifact chain is internally consistent and P5 parser v4
reproduces the stored technical result.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from orca_agent.application.errors import StateIntegrityError
from orca_agent.domain.ids import (
    ActionId,
    ArtifactId,
    EvidenceId,
    RunId,
    WorkflowRecordId,
    new_id,
)
from orca_agent.domain.p5 import (
    GeometryRecord,
    P5ActionRecord,
    P5ExecutionBinding,
    P5ExecutionPlan,
    P5JobRecord,
    P5JobStatus,
    P5NodeKind,
    P5ParseStatus,
    P5ResultRecord,
    P5WorkflowState,
)
from orca_agent.domain.p6 import (
    MethodContext,
    P6ArtifactRef,
    P6EvidenceRecord,
    P6EvidenceType,
    P6Locator,
    P6SourceOrigin,
    P6SourceResultRef,
    P6SourceSnapshot,
    ThermochemistryContext,
)
from orca_agent.execution.orca_parser import ParsedOrcaResult, parse_orca_output
from orca_agent.infrastructure.artifacts import ArtifactStore
from orca_agent.infrastructure.p3_records import ArtifactRecordRepository, StoredArtifact
from orca_agent.infrastructure.p5_records import LocalJobRepository, P5RecordRepository
from orca_agent.orchestration.p5_versions import P5_PARSER_VERSION

from .p6_observations import (
    ParsedP6Observations,
    parse_hessian_observations,
    parse_stdout_observations,
)


@dataclass(frozen=True)
class P6ResultBundle:
    """One result and every object needed to re-verify it offline."""

    source_ref: P6SourceResultRef
    result: P5ResultRecord
    action: P5ActionRecord
    binding: P5ExecutionBinding
    geometry: GeometryRecord
    job: P5JobRecord
    parsed: ParsedOrcaResult | None
    observations: ParsedP6Observations | None
    artifacts: tuple[StoredArtifact, ...]
    artifact_bytes: tuple[tuple[ArtifactId, bytes], ...]

    def bytes_for(self, artifact_id: ArtifactId) -> bytes:
        for current_id, value in self.artifact_bytes:
            if current_id == artifact_id:
                return value
        raise StateIntegrityError("source artifact bytes are missing from the result bundle")


@dataclass(frozen=True)
class P6Ingestion:
    snapshot: P6SourceSnapshot
    results: tuple[P6ResultBundle, ...]
    method_context: MethodContext
    thermochemistry_context: ThermochemistryContext | None
    evidence: tuple[P6EvidenceRecord, ...]


def load_source_bundle(
    *,
    connection,
    state_root: str | Path,
    source_run_id: RunId,
    expected_snapshot: P6SourceSnapshot | None = None,
    expected_revision: int | None = None,
    _selected_result_id: WorkflowRecordId | None = None,
    _visited: tuple[RunId, ...] = (),
) -> P6Ingestion:
    """Load and verify a P5 source, optionally against a frozen P6 snapshot."""

    from orca_agent.infrastructure.repositories import EventRepository, RunRepository

    if source_run_id in _visited:
        raise StateIntegrityError("P5 upstream reference cycle")
    _visited = (*_visited, source_run_id)

    runs = RunRepository(connection)
    events = EventRepository(connection)
    source = runs.get_verified(source_run_id, events)
    if not isinstance(source.state, P5WorkflowState):
        raise StateIntegrityError("P6 source must be a schema-4 P5 run")
    if (source.state.phase.value, source.state.status.value) not in {
        ("completed", "ready"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
    }:
        raise StateIntegrityError("P6 source P5 run is not technically completed")
    if expected_revision is not None and source.revision != expected_revision:
        raise StateIntegrityError("P6 source revision does not match the requested snapshot")
    if expected_snapshot is not None:
        if expected_snapshot.source_p5_run_id != source_run_id:
            raise StateIntegrityError("frozen P6 snapshot points at another P5 run")
        if source.revision < expected_snapshot.source_revision:
            raise StateIntegrityError("P5 source is shorter than the frozen snapshot")
        frozen_head = events.get(expected_snapshot.source_event_head_id)
        if frozen_head is None or frozen_head.run_id != source_run_id:
            raise StateIntegrityError("frozen P5 source event head is missing")
        if frozen_head.event_hash != expected_snapshot.source_event_head_hash:
            raise StateIntegrityError("frozen P5 source event head hash changed")

    records = P5RecordRepository(connection)
    entries = records.list_p5_for_run(source_run_id)
    plan = records.get_exact_p5(
        run_id=source_run_id,
        record_id=source.state.execution_plan_id,
        record_type="p5.execution_plan",
        model_type=P5ExecutionPlan,
    )
    if plan is None:
        raise StateIntegrityError("P5 execution plan is missing from the source closure")
    actions = _unique_by_action(entries)
    bindings = {
        item.record_id: item
        for _record_id, record_type, item in entries
        if record_type == "p5.execution_binding" and isinstance(item, P5ExecutionBinding)
    }
    geometries = tuple(
        item
        for _record_id, record_type, item in entries
        if record_type == "p5.geometry" and isinstance(item, GeometryRecord)
    )
    results = tuple(
        item
        for _record_id, record_type, item in entries
        if record_type == "p5.result" and isinstance(item, P5ResultRecord)
    )
    if expected_snapshot is not None:
        selected_ids = {item.result_id for item in expected_snapshot.results}
        results = tuple(item for item in results if item.record_id in selected_ids)
    if _selected_result_id is not None:
        results = tuple(item for item in results if item.record_id == _selected_result_id)
    if not results:
        raise StateIntegrityError("P5 source contains no result records")

    node_by_id = {node.node_id: node for node in plan.nodes}
    result_by_action: dict[ActionId, P5ResultRecord] = {}
    bundles: list[P6ResultBundle] = []
    artifact_repo = ArtifactRecordRepository(connection)
    artifact_store = ArtifactStore(state_root)
    for result in _ordered_results(results, node_by_id, actions):
        action = actions.get(result.action_id)
        if action is None:
            raise StateIntegrityError("P5 result points at a missing action")
        if result.action_id in result_by_action:
            raise StateIntegrityError("P5 source contains duplicate results for one action")
        result_by_action[result.action_id] = result
        binding = bindings.get(action.binding_id)
        if binding is None:
            raise StateIntegrityError("P5 action points at a missing binding")
        _verify_action_binding(source_run_id, result, action, binding)
        node = node_by_id.get(action.node_id)
        if node is None or node.kind is not result.primitive:
            raise StateIntegrityError("P5 result primitive is not bound to its plan node")
        geometry = _geometry_for_binding(geometries, binding)
        if binding.upstream_result_id is not None and not any(
            item.record_id == binding.upstream_result_id for item in results
        ):
            owner = connection.execute(
                "SELECT run_id FROM workflow_records WHERE record_id = ? "
                "AND record_type = 'p5.result'",
                (str(binding.upstream_result_id),),
            ).fetchone()
            if owner is None:
                raise StateIntegrityError("explicit upstream Opt result is missing")
            upstream_bundle = load_source_bundle(
                connection=connection,
                state_root=state_root,
                source_run_id=RunId(owner[0]),
                _selected_result_id=binding.upstream_result_id,
                _visited=_visited,
            )
            upstream = upstream_bundle.results[-1]
            if (
                upstream.result.primitive is not P5NodeKind.OPT
                or upstream.result.result_hash != binding.upstream_result_hash
                or upstream.result.parse_status is not P5ParseStatus.COMPLETE
                or upstream.geometry.identity_hash != geometry.identity_hash
                or upstream.binding.method_profile_hash != binding.method_profile_hash
            ):
                raise StateIntegrityError("explicit upstream Opt binding is invalid")
            from orca_agent.identity.geometry import parse_xyz_bytes

            symbols, coordinates = parse_xyz_bytes(
                upstream.bytes_for(upstream.source_ref.optimized_geometry_artifact_id)
            )
            if symbols != geometry.atom_symbols or any(
                abs(a - b) > 2e-6
                for left, right in zip(coordinates, geometry.coordinates, strict=True)
                for a, b in zip(left, right, strict=True)
            ):
                raise StateIntegrityError("external Opt XYZ does not match Freq geometry")
            bundles.extend(upstream_bundle.results)
        else:
            _verify_upstream(result, binding, result_by_action, results)
        job = LocalJobRepository(connection).get_by_execution(result.execution_id)
        if job is None:
            raise StateIntegrityError("P5 result execution has no local job")
        if (
            job.run_id != source_run_id
            or job.action_id != action.action_id
            or job.job_id != result.job_id
            or job.binding_id != binding.record_id
            or job.binding_hash != binding.binding_hash
            or job.execution_id != result.execution_id
        ):
            raise StateIntegrityError("P5 result job binding is inconsistent")
        if result.parse_status is P5ParseStatus.COMPLETE and (
            job.status is not P5JobStatus.SUCCEEDED
            or job.terminal_receipt_id != result.record_id
            or job.terminal_receipt_hash != result.result_hash
        ):
            raise StateIntegrityError("complete P5 result has no matching terminal receipt")
        if result.parse_status is not P5ParseStatus.COMPLETE and (
            job.status.value not in {"failed", "cancelled", "interrupted", "timed_out"}
            or job.terminal_receipt_id != result.record_id
            or job.terminal_receipt_hash != result.result_hash
        ):
            raise StateIntegrityError("diagnostic P5 source has no confirmed terminal receipt")

        artifact_values: list[StoredArtifact] = []
        artifact_bytes: list[tuple[ArtifactId, bytes]] = []

        def load_artifact(
            artifact_id: ArtifactId | None,
            role: str,
            *,
            required: bool,
            bound_action=action,
            bound_result=result,
            bound_artifact_values=artifact_values,
            bound_artifact_bytes=artifact_bytes,
        ) -> StoredArtifact | None:
            if artifact_id is None:
                if required:
                    raise StateIntegrityError(f"P5 result is missing its {role} artifact")
                return None
            artifact = artifact_repo.get(artifact_id)
            if artifact is None or artifact.run_id != source_run_id:
                raise StateIntegrityError(f"P5 {role} artifact is missing or has the wrong owner")
            if artifact.action_id is not None and artifact.action_id != bound_action.action_id:
                raise StateIntegrityError(f"P5 {role} artifact action owner is invalid")
            if (
                artifact.execution_id is not None
                and artifact.execution_id != bound_result.execution_id
            ):
                raise StateIntegrityError(f"P5 {role} artifact execution owner is invalid")
            data = artifact_store.read(artifact)
            bound_artifact_values.append(artifact)
            bound_artifact_bytes.append((artifact.artifact_id, data))
            return artifact

        geometry_artifact = load_artifact(binding.geometry_artifact_id, "geometry", required=True)
        input_artifact = load_artifact(action.input_artifact_id, "input", required=True)
        stdout_artifact = load_artifact(result.stdout_artifact_id, "stdout", required=True)
        stderr_artifact = load_artifact(result.stderr_artifact_id, "stderr", required=True)
        optimized_artifact = load_artifact(
            result.optimized_geometry_artifact_id, "optimized XYZ", required=False
        )
        hessian_artifact = load_artifact(result.hessian_artifact_id, "Hessian", required=False)
        assert (
            geometry_artifact is not None
            and input_artifact is not None
            and stdout_artifact is not None
            and stderr_artifact is not None
        )
        if geometry_artifact.content_hash != binding.xyz_bytes_sha256:
            raise StateIntegrityError("P5 geometry artifact hash does not match its binding")
        if input_artifact.content_hash != binding.input_sha256:
            raise StateIntegrityError("P5 input artifact hash does not match its binding")
        parsed: ParsedOrcaResult | None = None
        observations: ParsedP6Observations | None = None
        if result.parse_status is P5ParseStatus.COMPLETE:
            parsed = _reparse_result(
                result=result,
                node=node,
                geometry=geometry,
                input_artifact=input_artifact,
                stdout_artifact=stdout_artifact,
                stderr_artifact=stderr_artifact,
                hessian_artifact=hessian_artifact,
                optimized_artifact=optimized_artifact,
                artifact_bytes=artifact_bytes,
            )
            _compare_p5_result(result, parsed)
            if (
                result.data_origin.value == "orca_local"
                and parsed.orca_version != binding.orca_version
            ):
                raise StateIntegrityError("parsed ORCA version differs from execution binding")
            if result.primitive is P5NodeKind.FREQ:
                if hessian_artifact is None:
                    raise StateIntegrityError("complete frequency result has no Hessian")
                observations = parse_hessian_observations(
                    _bytes(artifact_bytes, hessian_artifact.artifact_id), allow_missing_modes=True
                )
                observations = parse_stdout_observations(
                    _bytes(artifact_bytes, stdout_artifact.artifact_id), observations
                )
        origin = P6SourceOrigin(result.data_origin.value)
        source_ref = P6SourceResultRef(
            result_id=result.record_id,
            result_hash=result.result_hash,
            action_id=result.action_id,
            action_hash=action.action_hash,
            binding_id=binding.record_id,
            binding_hash=binding.binding_hash,
            execution_id=result.execution_id,
            job_id=result.job_id,
            primitive=result.primitive.value,
            data_origin=origin,
            input_manifest_hash=result.input_manifest_hash,
            output_manifest_hash=result.output_manifest_hash,
            orca_version=result.orca_version,
            energy=result.energy if parsed is not None else None,
            energy_token=result.energy_token if parsed is not None else None,
            energy_unit="Eh" if parsed is not None else None,
            frequencies=() if observations is None else observations.frequencies,
            frequency_tokens=() if observations is None else observations.frequency_tokens,
            optimized_geometry_artifact_id=None
            if optimized_artifact is None
            else optimized_artifact.artifact_id,
            optimized_geometry_hash=None
            if optimized_artifact is None
            else optimized_artifact.content_hash,
            hessian_artifact_id=None if hessian_artifact is None else hessian_artifact.artifact_id,
            hessian_hash=None if hessian_artifact is None else hessian_artifact.content_hash,
            stdout_artifact_id=stdout_artifact.artifact_id,
            stdout_hash=stdout_artifact.content_hash,
            stderr_artifact_id=stderr_artifact.artifact_id,
            stderr_hash=stderr_artifact.content_hash,
            input_artifact_id=input_artifact.artifact_id,
            input_hash=input_artifact.content_hash,
            geometry_hash=binding.geometry_hash,
            upstream_result_id=binding.upstream_result_id,
            upstream_result_hash=binding.upstream_result_hash,
            parse_status=result.parse_status.value,
            integrity_verified=True,
        )
        bundles.append(
            P6ResultBundle(
                source_ref=source_ref,
                result=result,
                action=action,
                binding=binding,
                geometry=geometry,
                job=job,
                parsed=parsed,
                observations=observations,
                artifacts=tuple(artifact_values),
                artifact_bytes=tuple(artifact_bytes),
            )
        )

    origins = {item.source_ref.data_origin for item in bundles}
    if len(origins) != 1:
        raise StateIntegrityError("P6 source cannot mix real and fixture result origins")
    first = bundles[0]
    for item in bundles[1:]:
        if (
            item.geometry.identity_hash != first.geometry.identity_hash
            or item.geometry.confirmed_molecule_id != first.geometry.confirmed_molecule_id
            or item.binding.method_profile_id != first.binding.method_profile_id
            or item.binding.method_profile_hash != first.binding.method_profile_hash
            or item.binding.feature_profile_hash != first.binding.feature_profile_hash
            or item.binding.orca_version != first.binding.orca_version
        ):
            raise StateIntegrityError("P5 result contexts are not mutually consistent")
    head = events.get(source.last_event_id)
    if head is None:
        raise StateIntegrityError("P5 source event head is missing")
    source_head_id = source.last_event_id
    source_revision = source.revision
    source_snapshot_id = new_id(WorkflowRecordId)
    source_record_id = new_id(WorkflowRecordId)
    if expected_snapshot is not None:
        source_head_id = expected_snapshot.source_event_head_id
        source_revision = expected_snapshot.source_revision
        source_snapshot_id = expected_snapshot.snapshot_id
        source_record_id = expected_snapshot.record_id
    artifacts = _unique_artifacts(source_run_id, bundles)
    snapshot = P6SourceSnapshot.create(
        record_id=source_record_id,
        snapshot_id=source_snapshot_id,
        source_p5_run_id=source_run_id,
        source_revision=source_revision,
        source_event_head_id=source_head_id,
        source_event_head_hash=(
            head.event_hash
            if expected_snapshot is None
            else expected_snapshot.source_event_head_hash
        ),
        source_origin=next(iter(origins)),
        confirmed_molecule_id=first.geometry.confirmed_molecule_id,
        identity_hash=first.geometry.identity_hash,
        canonical_isomeric_smiles=first.geometry.canonical_isomeric_smiles,
        molecular_formula=first.geometry.molecular_formula,
        formal_charge=first.geometry.formal_charge,
        multiplicity=first.geometry.multiplicity,
        method_profile_id=first.binding.method_profile_id,
        method_profile_hash=first.binding.method_profile_hash,
        protocol_id=plan.protocol_id,
        results=tuple(item.source_ref for item in bundles),
        artifacts=artifacts,
    )
    if expected_snapshot is not None and snapshot != expected_snapshot:
        raise StateIntegrityError("P5 source closure no longer matches the frozen snapshot")
    method_context = _method_context(first, bundles, plan)
    thermo = _thermochemistry_context(bundles)
    evidence = _build_evidence(snapshot, bundles, method_context, thermo)
    return P6Ingestion(snapshot, tuple(bundles), method_context, thermo, evidence)


def _unique_by_action(
    entries: Iterable[tuple[WorkflowRecordId, str, object]],
) -> dict[ActionId, P5ActionRecord]:
    values: dict[ActionId, P5ActionRecord] = {}
    for _record_id, record_type, item in entries:
        if record_type != "p5.action" or not isinstance(item, P5ActionRecord):
            continue
        if item.action_id in values:
            raise StateIntegrityError("P5 source contains duplicate action IDs")
        values[item.action_id] = item
    return values


def _ordered_results(
    results: tuple[P5ResultRecord, ...],
    nodes: dict[str, object],
    actions: dict[ActionId, P5ActionRecord],
) -> tuple[P5ResultRecord, ...]:
    order = {node_id: index for index, node_id in enumerate(nodes)}
    try:
        return tuple(sorted(results, key=lambda item: order[actions[item.action_id].node_id]))
    except KeyError as error:
        raise StateIntegrityError("P5 result action is not in its execution plan") from error


def _verify_action_binding(
    source_run_id: RunId,
    result: P5ResultRecord,
    action: P5ActionRecord,
    binding: P5ExecutionBinding,
) -> None:
    if (
        result.run_id != source_run_id
        or action.run_id != source_run_id
        or binding.run_id != source_run_id
        or action.action_id != result.action_id
        or action.binding_id != binding.record_id
        or action.binding_hash != binding.binding_hash
        or binding.action_id != action.action_id
        or result.execution_id is None
        or result.input_manifest_hash != binding.input_manifest_hash
    ):
        raise StateIntegrityError("P5 result, action and binding hashes are inconsistent")


def _geometry_for_binding(
    geometries: tuple[GeometryRecord, ...], binding: P5ExecutionBinding
) -> GeometryRecord:
    matches = tuple(item for item in geometries if item.geometry_hash == binding.geometry_hash)
    if len(matches) != 1:
        raise StateIntegrityError("P5 binding does not select exactly one geometry record")
    geometry = matches[0]
    if (
        geometry.confirmed_molecule_id != binding.confirmed_molecule_id
        or geometry.identity_hash != binding.confirmed_molecule_hash
    ):
        raise StateIntegrityError("P5 binding geometry identity is inconsistent")
    return geometry


def _verify_upstream(
    result: P5ResultRecord,
    binding: P5ExecutionBinding,
    selected: dict[ActionId, P5ResultRecord],
    all_results: tuple[P5ResultRecord, ...],
) -> None:
    if binding.upstream_result_id is None:
        return
    upstream = next(
        (item for item in all_results if item.record_id == binding.upstream_result_id), None
    )
    if upstream is None or binding.upstream_result_hash != upstream.result_hash:
        raise StateIntegrityError("P5 upstream result reference is missing or changed")
    if upstream.run_id != result.run_id or upstream.record_id == result.record_id:
        raise StateIntegrityError("P5 upstream result reference is invalid")
    if upstream.action_id not in selected and upstream.record_id != binding.upstream_result_id:
        # This branch protects expected-snapshot subsets from silently using a
        # result outside the fixed closure.
        raise StateIntegrityError("P5 upstream result is outside the frozen closure")


def _reparse_result(
    *,
    result: P5ResultRecord,
    node,
    geometry: GeometryRecord,
    input_artifact: StoredArtifact,
    stdout_artifact: StoredArtifact,
    stderr_artifact: StoredArtifact,
    hessian_artifact: StoredArtifact | None,
    optimized_artifact: StoredArtifact | None,
    artifact_bytes: list[tuple[ArtifactId, bytes]],
) -> ParsedOrcaResult:
    return parse_orca_output(
        _bytes(artifact_bytes, stdout_artifact.artifact_id),
        primitive=node,
        geometry=geometry,
        input_manifest_hash=result.input_manifest_hash,
        exit_code=0 if result.exit_code is None else result.exit_code,
        hessian_bytes=None
        if hessian_artifact is None
        else _bytes(artifact_bytes, hessian_artifact.artifact_id),
        optimized_xyz_bytes=None
        if optimized_artifact is None
        else _bytes(artifact_bytes, optimized_artifact.artifact_id),
        data_origin=result.data_origin,
        stderr_bytes=_bytes(artifact_bytes, stderr_artifact.artifact_id),
    )


def _compare_p5_result(stored: P5ResultRecord, parsed: ParsedOrcaResult) -> None:
    pairs = (
        (parsed.parser_version, P5_PARSER_VERSION),
        (parsed.orca_version, stored.orca_version),
        (parsed.primitive, stored.primitive),
        (parsed.data_origin, stored.data_origin),
        (parsed.input_manifest_hash, stored.input_manifest_hash),
        (parsed.output_manifest_hash, stored.output_manifest_hash),
        (parsed.normal_termination, stored.normal_termination),
        (parsed.scf_converged, stored.scf_converged),
        (parsed.optimization_converged, stored.optimization_converged),
        (parsed.energy, stored.energy),
        (parsed.energy_token, stored.energy_token),
        (parsed.energy_unit, stored.energy_unit),
        (parsed.frequencies, stored.frequencies),
        (parsed.frequency_unit, stored.frequency_unit),
        (parsed.parse_status, stored.parse_status),
    )
    if any(left != right for left, right in pairs):
        raise StateIntegrityError("source_reparse_mismatch")


def _method_context(
    first: P6ResultBundle, bundles: tuple[P6ResultBundle, ...], plan: P5ExecutionPlan
) -> MethodContext:
    versions = {item.result.orca_version for item in bundles}
    settings = {item.binding.feature_profile_hash for item in bundles}
    return MethodContext.create(
        confirmed_molecule_id=first.geometry.confirmed_molecule_id,
        identity_hash=first.geometry.identity_hash,
        canonical_isomeric_smiles=first.geometry.canonical_isomeric_smiles,
        molecular_formula=first.geometry.molecular_formula,
        atom_symbols=first.geometry.atom_symbols,
        formal_charge=first.geometry.formal_charge,
        multiplicity=first.geometry.multiplicity,
        method_profile_id=first.binding.method_profile_id,
        method_profile_hash=first.binding.method_profile_hash,
        orca_version=next(iter(versions)) if len(versions) == 1 else None,
        protocol_id=plan.protocol_id,
        protocol_hash=plan.protocol_hash,
        settings_hash=next(iter(settings)) if len(settings) == 1 else None,
    )


def _thermochemistry_context(bundles: tuple[P6ResultBundle, ...]) -> ThermochemistryContext | None:
    frequency = next(
        (
            item
            for item in bundles
            if item.result.primitive is P5NodeKind.FREQ and item.observations is not None
        ),
        None,
    )
    if frequency is None or frequency.observations is None:
        return None
    thermo = frequency.observations.thermochemistry
    stdout = frequency.source_ref.stdout_artifact_id
    locators = {
        name: {"artifact_id": str(stdout), "span": list(span)}
        for name, span in thermo.spans.items()
    }
    missing = list(thermo.missing_reasons)
    return ThermochemistryContext.create(
        temperature_K=thermo.temperature_K,
        pressure_atm=thermo.pressure_atm,
        quasi_rrho=thermo.quasi_rrho,
        cutoff_frequency_cm1=thermo.cutoff_frequency_cm1,
        frequency_scale_factor=frequency.observations.stdout_scale_factor
        if frequency.observations.stdout_scale_factor is not None
        else frequency.observations.scale_factor,
        qrrho_reference_frequency_cm1=thermo.qrrho_reference_frequency_cm1,
        standard_state=thermo.standard_state,
        symmetry_number=thermo.symmetry_number,
        field_locators=locators,
        missing_reasons=tuple(missing),
    )


def _build_evidence(
    snapshot: P6SourceSnapshot,
    bundles: tuple[P6ResultBundle, ...],
    method: MethodContext,
    thermo: ThermochemistryContext | None,
) -> tuple[P6EvidenceRecord, ...]:
    values: list[P6EvidenceRecord] = []
    first = bundles[0]
    values.append(
        P6EvidenceRecord.create(
            record_id=new_id(WorkflowRecordId),
            evidence_id=new_id(EvidenceId),
            source_p5_run_id=first.result.run_id,
            source_result_id=first.source_ref.result_id,
            source_execution_id=first.source_ref.execution_id,
            quantity="method_context",
            evidence_type=P6EvidenceType.METHOD_CONTEXT,
            value=None,
            raw_value_token=None,
            unit=None,
            locator=P6Locator(
                artifact_id=first.source_ref.input_artifact_id,
                artifact_hash=first.source_ref.input_hash,
                block="p5.execution_binding.input_manifest",
            ),
            source_artifact_id=first.source_ref.input_artifact_id,
            source_artifact_hash=first.source_ref.input_hash,
            source_origin=snapshot.source_origin,
            context_hash=method.context_hash,
            context=method,
        )
    )
    if thermo is not None:
        frequency = next(item for item in bundles if item.observations is not None)
        values.append(
            P6EvidenceRecord.create(
                record_id=new_id(WorkflowRecordId),
                evidence_id=new_id(EvidenceId),
                source_p5_run_id=frequency.result.run_id,
                source_result_id=frequency.source_ref.result_id,
                source_execution_id=frequency.source_ref.execution_id,
                quantity="thermochemistry_context",
                evidence_type=P6EvidenceType.THERMOCHEMISTRY_CONTEXT,
                locator=P6Locator(
                    artifact_id=frequency.source_ref.stdout_artifact_id,
                    artifact_hash=frequency.source_ref.stdout_hash,
                    block="THERMOCHEMISTRY",
                ),
                source_artifact_id=frequency.source_ref.stdout_artifact_id,
                source_artifact_hash=frequency.source_ref.stdout_hash,
                source_origin=snapshot.source_origin,
                context_hash=thermo.context_hash,
                context=thermo,
            )
        )
    for bundle in bundles:
        if bundle.parsed is None:
            values.append(
                P6EvidenceRecord.create(
                    record_id=new_id(WorkflowRecordId),
                    evidence_id=new_id(EvidenceId),
                    source_p5_run_id=bundle.result.run_id,
                    source_result_id=bundle.result.record_id,
                    source_execution_id=bundle.result.execution_id,
                    quantity="execution_fact",
                    evidence_type=P6EvidenceType.EXECUTION_FACT,
                    value=bundle.job.status.value,
                    raw_value_token=bundle.job.status.value,
                    locator=P6Locator(
                        artifact_id=bundle.source_ref.stdout_artifact_id,
                        artifact_hash=bundle.source_ref.stdout_hash,
                        block="archived_terminal_output",
                    ),
                    source_artifact_id=bundle.source_ref.stdout_artifact_id,
                    source_artifact_hash=bundle.source_ref.stdout_hash,
                    source_origin=snapshot.source_origin,
                )
            )
            continue
        energy = bundle.parsed
        stdout_bytes = bundle.bytes_for(bundle.source_ref.stdout_artifact_id)
        text = stdout_bytes.decode("utf-8")
        span = _token_span(text, energy.energy_token, bundle.result.source_locations.get("energy"))
        values.append(
            P6EvidenceRecord.create(
                record_id=new_id(WorkflowRecordId),
                evidence_id=new_id(EvidenceId),
                source_p5_run_id=bundle.result.run_id,
                source_result_id=bundle.source_ref.result_id,
                source_execution_id=bundle.source_ref.execution_id,
                quantity="electronic_energy",
                evidence_type=P6EvidenceType.ELECTRONIC_ENERGY,
                value=energy.energy,
                raw_value_token=energy.energy_token,
                unit="Eh",
                locator=P6Locator(
                    artifact_id=bundle.source_ref.stdout_artifact_id,
                    artifact_hash=bundle.source_ref.stdout_hash,
                    utf8_character_span=span,
                    block="FINAL SINGLE POINT ENERGY",
                ),
                source_artifact_id=bundle.source_ref.stdout_artifact_id,
                source_artifact_hash=bundle.source_ref.stdout_hash,
                source_origin=snapshot.source_origin,
                context_hash=method.context_hash,
            )
        )
        if bundle.observations is None:
            continue
        for mode_index, (value, token, mode_span) in enumerate(
            zip(
                bundle.observations.frequencies,
                bundle.observations.frequency_tokens,
                bundle.observations.frequency_spans,
                strict=True,
            )
        ):
            values.append(
                P6EvidenceRecord.create(
                    record_id=new_id(WorkflowRecordId),
                    evidence_id=new_id(EvidenceId),
                    source_p5_run_id=bundle.result.run_id,
                    source_result_id=bundle.source_ref.result_id,
                    source_execution_id=bundle.source_ref.execution_id,
                    quantity="vibrational_frequency",
                    evidence_type=P6EvidenceType.VIBRATIONAL_FREQUENCY,
                    value=value,
                    raw_value_token=token,
                    unit="cm^-1",
                    locator=P6Locator(
                        artifact_id=bundle.source_ref.hessian_artifact_id,
                        artifact_hash=bundle.source_ref.hessian_hash,
                        utf8_character_span=mode_span,
                        block="vibrational_frequencies",
                        index=mode_index,
                    ),
                    source_artifact_id=bundle.source_ref.hessian_artifact_id,
                    source_artifact_hash=bundle.source_ref.hessian_hash,
                    source_origin=snapshot.source_origin,
                    context_hash=method.context_hash,
                )
            )
    return tuple(values)


def _unique_artifacts(
    source_run_id: RunId, bundles: tuple[P6ResultBundle, ...]
) -> tuple[P6ArtifactRef, ...]:
    values: dict[ArtifactId, P6ArtifactRef] = {}
    roles = {
        "geometry_artifact_id": "geometry",
        "input_artifact_id": "input",
        "stdout_artifact_id": "stdout",
        "stderr_artifact_id": "stderr",
        "optimized_geometry_artifact_id": "optimized_xyz",
        "hessian_artifact_id": "hessian",
    }
    for bundle in bundles:
        for artifact in bundle.artifacts:
            role = next(
                (
                    role
                    for field, role in roles.items()
                    if getattr(bundle.source_ref, field, None) == artifact.artifact_id
                ),
                "source",
            )
            values.setdefault(
                artifact.artifact_id,
                P6ArtifactRef(
                    artifact_id=artifact.artifact_id,
                    content_hash=artifact.content_hash,
                    size_bytes=artifact.size_bytes,
                    media_type=artifact.media_type,
                    role=role,
                    relative_path=artifact.relative_path,
                    owner_run_id=artifact.run_id,
                    owner_action_id=artifact.action_id,
                    owner_execution_id=artifact.execution_id,
                ),
            )
    return tuple(values.values())


def _bytes(values: list[tuple[ArtifactId, bytes]], artifact_id: ArtifactId) -> bytes:
    for current, value in values:
        if current == artifact_id:
            return value
    raise StateIntegrityError("artifact bytes are missing")


def _token_span(text: str, token: str, hint: object) -> tuple[int, int]:
    start = hint if type(hint) is int and hint >= 0 else 0
    index = text.find(token, start)
    if index < 0:
        index = text.rfind(token)
    if index < 0:
        raise StateIntegrityError("raw energy token is not locatable in stdout")
    return index, index + len(token)


__all__ = ["P6Ingestion", "P6ResultBundle", "load_source_bundle"]
