"""P7 preparation boundary for identity, geometry, and input snapshots.

The service deliberately stops before execution approval.  It may create a
P4 identity run so the provider response is durably auditable, but it never
confirms identity, approves a P5 action, or launches ORCA.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import timedelta
from pathlib import Path

from orca_agent.application.p4_service import P4ApplicationService
from orca_agent.application.p7_runtime_config import P7RuntimeConfig
from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import ConversationId, RunId, new_id
from orca_agent.domain.json_types import thaw_json
from orca_agent.domain.p4 import IdentityProvider, MoleculeInputKind, P4Phase
from orca_agent.domain.p5 import (
    GeometryDraft,
    GeometryRecord,
    P5ExecutionNode,
    P5GeometrySource,
    P5NodeKind,
)
from orca_agent.domain.p7_preparation import (
    PREPARATION_SCHEMA_VERSION,
    PreparationStatus,
    PreparedCalculation,
)
from orca_agent.domain.p7_task import TaskRecord
from orca_agent.execution.orca_compiler import compile_orca_input
from orca_agent.identity.geometry import generate_geometry_draft, validate_xyz_bytes
from orca_agent.identity.ports import IdentityLookupRequest, LookupResult, normalize_input
from orca_agent.identity.rdkit_normalizer import RDKitNormalizer
from orca_agent.infrastructure.clock import Clock, SystemClock
from orca_agent.orchestration.p4_commands import StartPlanningRun
from orca_agent.orchestration.p4_versions import P4_ENGINE_VERSION, P4_SCHEMA_VERSION
from orca_agent.planning.p5_protocols import get_p5_protocol
from orca_agent.planning.registry import METHOD_R2SCAN3C


class P7PreparationService:
    """Build one immutable preparation snapshot for a P7 task generation."""

    confirmation_ttl = timedelta(hours=24)

    def __init__(
        self,
        state_root: str | Path,
        *,
        p4_service: P4ApplicationService,
        runtime_config: P7RuntimeConfig,
        clock: Clock | None = None,
    ) -> None:
        self.state_root = Path(state_root).resolve()
        self.p4 = p4_service
        self.runtime_config = runtime_config
        self.clock = clock or SystemClock()
        self.normalizer = RDKitNormalizer()

    def prepare(
        self,
        task: TaskRecord,
        *,
        generation: int,
        source_task: TaskRecord | None = None,
        source_geometry: GeometryRecord | None = None,
        source_geometry_bytes: bytes | None = None,
        source_required: bool = False,
        source_reference: str | None = None,
        source_result_id: str | None = None,
    ) -> PreparedCalculation:
        """Prepare identity, geometry, and the first closed ORCA input.

        ``generation`` is supplied by the task projection and is never
        inferred from an existing snapshot.  A caller can therefore retry a
        failed preparation without mutating a prior immutable record.
        """

        if task.request is None or task.validation is None or task.plan is None:
            raise ValueError("P7 preparation requires a validated task plan")
        now = self.clock.now_utc()
        base = {
            "schema_version": PREPARATION_SCHEMA_VERSION,
            "prepared_id": f"prepared_{uuid.uuid4().hex}",
            "task_id": task.task_id,
            "conversation_id": task.conversation_id,
            "preparation_generation": generation,
            "task_revision": task.revision,
            "request_hash": task.request.request_hash,
            "validation_hash": task.validation.validation_hash,
            "plan_hash": task.plan.plan_hash,
            "parameter_snapshot": self._parameter_snapshot(task),
            "plan_snapshot": task.plan.model_dump(mode="json"),
            "output_spec_snapshot": task.request.output_spec.model_dump(mode="json"),
            "created_at_utc": now,
            "updated_at_utc": now,
            "confirmation_expires_at_utc": now + self.confirmation_ttl,
        }
        # An explicit historical selector is a hard binding.  If its task or
        # completed Opt geometry cannot be resolved, stop preparation rather
        # than silently switching to the current task's fresh RDKit geometry.
        if source_required and (
            source_task is None or source_geometry is None or source_geometry_bytes is None
        ):
            return PreparedCalculation.create(
                **base,
                status=PreparationStatus.FAILED,
                dependencies={
                    "source_required": True,
                    "source_reference": source_reference,
                    "source_result_id": source_result_id,
                    "source_task_id": None if source_task is None else source_task.task_id,
                },
                readiness={
                    "identity": "blocked",
                    "geometry": (
                        "source_bytes_unavailable"
                        if source_geometry is not None
                        else "source_unavailable"
                    ),
                    "first_input": "blocked",
                },
                error_code="source_geometry_unavailable",
                error_message=("the explicitly selected historical Opt geometry is unavailable"),
            )
        try:
            identity, p4_run_id, p4_command_id = self._prepare_identity(
                task, source_task=source_task
            )
            if identity["candidate"] is None:
                return PreparedCalculation.create(
                    **base,
                    status=PreparationStatus.NEEDS_CLARIFICATION,
                    identity_snapshot=identity,
                    dependencies={"p4_run_id": p4_run_id, "p4_command_id": p4_command_id},
                    readiness={
                        "identity": "needs_clarification",
                        "geometry": "blocked",
                        "first_input": "blocked",
                    },
                    error_code="identity_not_unique",
                    error_message="PubChem/RDKit did not yield one confirmable molecule identity",
                )

            candidate = identity["candidate"]
            if not isinstance(candidate, dict):
                raise ValueError("identity candidate snapshot is invalid")
            inspection = self.normalizer.inspect_structure(
                str(candidate["canonical_isomeric_smiles"])
            )
            charge = (
                int(task.request.charge.value)
                if task.request.charge is not None
                else inspection.formal_charge
            )
            multiplicity = (
                int(task.request.multiplicity.value) if task.request.multiplicity is not None else 1
            )
            if inspection.formal_charge != charge:
                raise ValueError("resolved identity formal charge differs from the task charge")
            structure_spec = {
                "canonical_isomeric_smiles": inspection.canonical_isomeric_smiles,
                "molecular_formula": inspection.molecular_formula,
                "formal_charge": inspection.formal_charge,
                "multiplicity": multiplicity,
                "structure_hash": inspection.structure_hash,
                "isotope_labels": list(inspection.isotope_labels),
            }
            if source_geometry is not None:
                if source_geometry_bytes is None:
                    raise ValueError("historical Opt geometry bytes are unavailable")
                validate_xyz_bytes(source_geometry_bytes, source_geometry)
                draft = self._draft_from_geometry(source_geometry, inspection.structure_hash)
                geometry_source = "history_opt"
            else:
                draft = generate_geometry_draft(structure_spec)
                geometry_source = "rdkit_initial"
            node, protocol = self._preview_node(task)
            preview_geometry = source_geometry if source_geometry is not None else draft
            preview_geometry_bytes = source_geometry_bytes if source_geometry is not None else None
            compiled = compile_orca_input(
                node,
                METHOD_R2SCAN3C,
                preview_geometry,
                node.budget,
                protocol.feature_profile(),
                geometry_bytes=preview_geometry_bytes,
            )
            preview = {
                "input_text": compiled.input_bytes.decode("utf-8"),
                "input_sha256": compiled.input_sha256,
                "geometry_sha256": compiled.geometry_sha256,
                "geometry_xyz_base64": base64.b64encode(compiled.geometry_bytes).decode("ascii"),
                "manifest": compiled.manifest,
                "manifest_hash": compiled.manifest_hash,
            }
            dependencies = {
                "p4_run_id": p4_run_id,
                "p4_command_id": p4_command_id,
                "source_task_id": None if source_task is None else source_task.task_id,
                "source_p5_run_id": None if source_task is None else source_task.p5_run_id,
                "source_reference": source_reference,
                "source_result_id": source_result_id,
                "algorithm": "PubChem identity -> RDKit inspect -> ETKDGv3 -> closed ORCA compiler",
                "p4_schema_version": P4_SCHEMA_VERSION,
                "p4_engine_version": P4_ENGINE_VERSION,
            }
            return PreparedCalculation.create(
                **base,
                status=PreparationStatus.READY,
                geometry_source=geometry_source,
                identity_snapshot=identity,
                geometry_draft=self._draft_snapshot(draft),
                geometry_hash=draft.geometry_hash,
                xyz_bytes_sha256=compiled.geometry_sha256,
                first_input_preview=preview,
                first_input_hash=sha256_hex(
                    {
                        "input_sha256": compiled.input_sha256,
                        "geometry_sha256": compiled.geometry_sha256,
                    }
                ),
                dependencies=dependencies,
                readiness={
                    "identity": "unique_candidate",
                    "rdkit": "inspected",
                    "geometry": "frozen",
                    "first_input": "compiled_preview_only",
                    "execution": "awaiting_single_final_confirmation",
                },
            )
        except Exception as error:
            return PreparedCalculation.create(
                **base,
                status=PreparationStatus.FAILED,
                error_code=type(error).__name__,
                error_message=str(error)[:4096] or "P7 preparation failed",
                readiness={"identity": "failed", "geometry": "failed", "first_input": "failed"},
            )

    def _prepare_identity(
        self, task: TaskRecord, *, source_task: TaskRecord | None
    ) -> tuple[dict[str, object], str | None, str | None]:
        request = task.request
        if request is None or request.molecule_kind is None or request.molecule_value is None:
            raise ValueError("molecule input is missing")
        if source_task is not None and source_task.p4_run_id:
            view = self.p4.inspect(RunId(source_task.p4_run_id))
            if view.confirmed_molecule is not None:
                confirmed = view.confirmed_molecule
                return (
                    {
                        "input": {
                            "kind": request.molecule_kind.value,
                            "value": request.molecule_value,
                        },
                        "candidate": {
                            "candidate_id": str(confirmed.candidate_id),
                            "canonical_isomeric_smiles": confirmed.canonical_isomeric_smiles,
                            "molecular_formula": confirmed.molecular_formula,
                            "formal_charge": confirmed.formal_charge,
                            "cid": confirmed.candidate_id,
                            "provider": confirmed.provider.value,
                        },
                        "confirmed_source_run_id": str(view.run_id),
                        "confirmed_identity_hash": confirmed.identity_record_hash,
                        "lookup_mode": "trusted_history",
                    },
                    str(view.run_id),
                    None,
                )

        input_kind = MoleculeInputKind(request.molecule_kind.value)
        normalized_value = normalize_input(input_kind, request.molecule_value)
        lookup: LookupResult | None = None
        raw_candidates = ()
        if input_kind is not MoleculeInputKind.SMILES:
            port = (
                self.p4.fake_adapter
                if self.runtime_config.identity_provider == "fake"
                else self.p4.pubchem_adapter
            )
            lookup = port.lookup_candidates(
                IdentityLookupRequest(input_kind=input_kind, normalized_value=normalized_value)
            )
            if not lookup.succeeded:
                return (
                    {
                        "input": {"kind": input_kind.value, "value": normalized_value},
                        "candidate": None,
                        "lookup": self._lookup_snapshot(lookup),
                    },
                    None,
                    None,
                )
            raw_candidates = lookup.candidates
            if len(raw_candidates) != 1:
                return (
                    {
                        "input": {"kind": input_kind.value, "value": normalized_value},
                        "candidate": None,
                        "candidate_count": len(raw_candidates),
                        "lookup": self._lookup_snapshot(lookup),
                    },
                    None,
                    None,
                )

        p4_run_id = new_id(RunId)
        p4_conversation_id = new_id(ConversationId)
        provider = (
            IdentityProvider.LOCAL
            if input_kind is MoleculeInputKind.SMILES
            else IdentityProvider.PUBCHEM
            if self.runtime_config.identity_provider == "pubchem"
            else IdentityProvider.FAKE
        )
        command = StartPlanningRun.create(
            input_kind=input_kind,
            raw_input=normalized_value,
            charge=int(request.charge.value) if request.charge is not None else 0,
            multiplicity=int(request.multiplicity.value) if request.multiplicity is not None else 1,
            provider=provider,
            run_id=p4_run_id,
            conversation_id=p4_conversation_id,
            requested_at_utc=self.clock.now_utc(),
            new_conversation=True,
        )
        started = self.p4.start(command)
        if not started.accepted:
            raise ValueError(f"P4 identity preparation failed: {started.code}")
        self.p4.create_worker().run_once(run_id=p4_run_id, limit=1)
        view = self.p4.inspect(p4_run_id)
        candidate = None
        if view.state.phase is P4Phase.AWAITING_IDENTITY and view.candidate_bundle is not None:
            if view.candidate_bundle.confirmable and len(view.candidate_bundle.candidates) == 1:
                item = view.candidate_bundle.candidates[0]
                candidate = {
                    "candidate_id": item.candidate_id,
                    "canonical_isomeric_smiles": item.canonical_isomeric_smiles,
                    "molecular_formula": item.molecular_formula,
                    "formal_charge": item.formal_charge,
                    "cid": item.cid,
                    "inchikey": item.inchikey,
                    "provider": item.provider.value,
                    "candidate_hash": item.candidate_hash,
                    "candidate_set_hash": view.candidate_bundle.candidate_set_hash,
                    "source_body_sha256": item.source_body_sha256,
                    "p4_run_id": str(p4_run_id),
                }
        return (
            {
                "input": {"kind": input_kind.value, "value": normalized_value},
                "candidate": candidate,
                "lookup": None if lookup is None else self._lookup_snapshot(lookup),
                "p4_run_id": str(p4_run_id),
                "p4_phase": view.state.phase.value,
                "candidate_count": 0
                if view.candidate_bundle is None
                else len(view.candidate_bundle.candidates),
            },
            str(p4_run_id),
            str(command.command_id),
        )

    def _preview_node(self, task: TaskRecord) -> tuple[P5ExecutionNode, object]:
        if task.plan is None or not task.plan.nodes:
            raise ValueError("task plan has no execution node")
        protocol = get_p5_protocol(task.plan.protocol_id)
        node_view = task.plan.nodes[0]
        kind = P5NodeKind(node_view.kind)
        return (
            P5ExecutionNode(
                node_id=f"{protocol.protocol_id}:node-1",
                kind=kind,
                geometry_source=(
                    P5GeometrySource.INITIAL
                    if node_view.geometry_source == "initial_geometry"
                    else P5GeometrySource.OPTIMIZED
                ),
                depends_on=(),
                source_node_id=None,
                method_profile_id=METHOD_R2SCAN3C.registry_id,
                method_profile_hash=METHOD_R2SCAN3C.entry_hash,
                budget=protocol.budget_for(kind),
            ),
            protocol,
        )

    @staticmethod
    def _parameter_snapshot(task: TaskRecord) -> dict[str, object]:
        request = task.request
        if request is None:
            return {}
        return {
            "method": None if request.method is None else request.method.model_dump(mode="json"),
            "environment": None
            if request.environment is None
            else request.environment.model_dump(mode="json"),
            "charge": None if request.charge is None else request.charge.model_dump(mode="json"),
            "multiplicity": None
            if request.multiplicity is None
            else request.multiplicity.model_dump(mode="json"),
            "operations": list(request.operations),
        }

    @staticmethod
    def _draft_snapshot(draft: GeometryDraft) -> dict[str, object]:
        value = draft.model_dump(mode="json")
        value.pop("xyz_bytes", None)
        value["xyz_bytes_base64"] = base64.b64encode(draft.xyz_bytes).decode("ascii")
        return value

    @staticmethod
    def draft_from_snapshot(value: object) -> GeometryDraft:
        """Rehydrate exactly one persisted geometry draft without embedding."""

        snapshot = thaw_json(value)
        if not isinstance(snapshot, dict):
            raise ValueError("geometry draft snapshot is not an object")
        encoded = snapshot.pop("xyz_bytes_base64", None)
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("geometry draft snapshot has no frozen XYZ bytes")
        try:
            xyz_bytes = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (ValueError, UnicodeEncodeError) as error:
            raise ValueError("geometry draft snapshot XYZ bytes are invalid") from error
        try:
            snapshot["atom_symbols"] = tuple(snapshot["atom_symbols"])
            snapshot["atom_map"] = tuple(snapshot["atom_map"])
            snapshot["coordinates"] = tuple(tuple(point) for point in snapshot["coordinates"])
        except (KeyError, TypeError) as error:
            raise ValueError("geometry draft snapshot atom fields are invalid") from error
        snapshot["xyz_bytes"] = xyz_bytes
        try:
            return GeometryDraft.model_validate(snapshot, strict=True)
        except (TypeError, ValueError) as error:
            raise ValueError("geometry draft snapshot is invalid") from error

    @staticmethod
    def _draft_from_geometry(geometry: GeometryRecord, structure_hash: str) -> GeometryDraft:
        xyz = geometry.xyz_bytes()
        return GeometryDraft.create(
            canonical_isomeric_smiles=geometry.canonical_isomeric_smiles,
            molecular_formula=geometry.molecular_formula,
            formal_charge=geometry.formal_charge,
            multiplicity=geometry.multiplicity,
            structure_hash=structure_hash,
            atom_symbols=geometry.atom_symbols,
            atom_map=geometry.atom_map,
            coordinates=geometry.coordinates,
            xyz_precision=geometry.xyz_precision,
            xyz_bytes=xyz,
            rdkit_version=geometry.rdkit_version,
            seed=geometry.seed,
            geometry_hash=geometry.geometry_hash,
        )

    @staticmethod
    def _lookup_snapshot(result: LookupResult) -> dict[str, object]:
        return {
            "provider": result.provider.value,
            "adapter_version": result.adapter_version,
            "body_sha256": hashlib.sha256(result.body).hexdigest(),
            "request_metadata": result.request_metadata,
            "candidate_count": len(result.candidates),
            "error_code": None if result.error_code is None else result.error_code.value,
        }


__all__ = ["P7PreparationService"]
