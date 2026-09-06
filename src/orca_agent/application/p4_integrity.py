"""Shared trusted reads for P4 identity records and response evidence."""

from orca_agent.application.errors import StateIntegrityError
from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.p4 import (
    CandidateBundle,
    ConfirmedMolecule,
    LookupAttempt,
    MoleculeQuery,
    PreparedPlan,
    ResponseEnvelope,
)
from orca_agent.domain.registry import RegistrySnapshot
from orca_agent.infrastructure.artifacts import ArtifactStore
from orca_agent.infrastructure.p3_records import ArtifactRecordRepository, P4RecordRepository
from orca_agent.orchestration.p4_versions import P4_NORMALIZER_VERSION
from orca_agent.planning.validator import (
    PlanValidationError,
    validate_ground_state_plan,
    validate_registry_snapshot,
)


def verified_attempts(connection, state, state_root):
    """Validate every attempt before scheduling or replaying a provider request."""
    records = P4RecordRepository(connection)
    effect = connection.execute(
        "SELECT attempt_count, source_event_id FROM outbox WHERE effect_id = ? AND run_id = ?",
        (str(state.identity_effect_id), str(state.run_id)),
    ).fetchone()
    if effect is None:
        raise StateIntegrityError("P4 identity effect is missing")
    entries = records.list_for_run(state.run_id)
    attempts = tuple(
        value
        for _, kind, value in entries
        if kind == "p4.lookup_attempt" and isinstance(value, LookupAttempt)
    )
    envelopes = {}
    for _, kind, envelope in entries:
        if kind != "p4.response_envelope":
            continue
        key = (envelope.generation, envelope.request_sequence)
        if (
            key in envelopes
            or envelope.run_id != state.run_id
            or envelope.query_id != state.query_id
            or envelope.query_hash != state.query_hash
            or envelope.effect_id != state.identity_effect_id
            or envelope.generation > effect[0]
            or envelope.request_sequence != 1
        ):
            raise StateIntegrityError("P4 response envelope owner or request key is invalid")
        envelopes[key] = envelope
    seen = set()
    for attempt in attempts:
        key = (attempt.generation, attempt.request_sequence)
        if (
            key in seen
            or attempt.run_id != state.run_id
            or attempt.query_id != state.query_id
            or attempt.query_hash != state.query_hash
            or attempt.effect_id != state.identity_effect_id
            or attempt.generation > effect[0]
            or attempt.request_sequence != 1
        ):
            raise StateIntegrityError("P4 attempt owner, query or generation is invalid")
        seen.add(key)
        _source(connection, attempt.record_id, effect[1])
        artifact = ArtifactRecordRepository(connection).get(attempt.response_artifact_id)
        if artifact is None or artifact.run_id != state.run_id:
            raise StateIntegrityError("P4 attempt source artifact is missing")
        try:
            envelope = ResponseEnvelope.model_validate_json(
                ArtifactStore(state_root).read(artifact)
            )
        except (ValueError, TypeError) as error:
            raise StateIntegrityError("P4 response envelope is invalid") from error
        persisted = records.get_exact(
            run_id=state.run_id,
            record_id=envelope.record_id,
            record_type="p4.response_envelope",
            model_type=ResponseEnvelope,
        )
        if (
            persisted != envelope
            or envelopes.get(key) != envelope
            or envelope.run_id != state.run_id
            or envelope.query_id != state.query_id
            or envelope.query_hash != state.query_hash
            or envelope.effect_id != state.identity_effect_id
            or envelope.generation != attempt.generation
            or envelope.request_sequence != attempt.request_sequence
            or envelope.provider != attempt.provider
            or envelope.adapter_version != attempt.adapter_version
            or envelope.envelope_hash != attempt.response_envelope_hash
            or envelope.body_sha256 != attempt.response_body_sha256
        ):
            raise StateIntegrityError("P4 attempt and response envelope disagree")
        _source(connection, envelope.record_id, effect[1])
    if set(envelopes) != seen:
        raise StateIntegrityError("P4 response envelope and attempt must be paired")
    return attempts


def _source(connection, record_id, event_id):
    row = connection.execute(
        "SELECT source_event_id FROM workflow_records WHERE record_id = ?", (str(record_id),)
    ).fetchone()
    if row is None or row[0] != str(event_id):
        raise StateIntegrityError("P4 record source event is inconsistent")


def verify_record_chain(uow, state, state_root):
    """Bind the phase-required record chain to an already replay-verified state."""
    records = P4RecordRepository(uow.connection)

    def load(identifier, kind, model):
        if identifier is None:
            return None
        record = records.get_exact(
            run_id=state.run_id,
            record_id=identifier,
            record_type=kind,
            model_type=model,
        )
        if record is None:
            raise StateIntegrityError(f"P4 required record is missing: {kind}")
        return record

    query = load(state.query_id, "p4.molecule_query", MoleculeQuery)
    registry = load(state.registry_snapshot_id, "p4.registry_snapshot", RegistrySnapshot)
    if (
        query.run_id != state.run_id
        or query.conversation_id != state.conversation_id
        or query.query_hash != state.query_hash
        or registry.snapshot_hash != state.registry_snapshot_hash
    ):
        raise StateIntegrityError("P4 query or registry differs from state")
    try:
        validate_registry_snapshot(registry)
    except PlanValidationError as error:
        raise StateIntegrityError("P4 registry is invalid") from error
    creation = uow.events.list_for_run(state.run_id)[0].event
    _source(uow.connection, query.query_id, creation.event_id)
    _source(uow.connection, registry.record_id, creation.event_id)
    attempts = verified_attempts(uow.connection, state, state_root)
    bundle = load(state.candidate_bundle_id, "p4.candidate_bundle", CandidateBundle)
    confirmed = load(state.confirmed_molecule_id, "p4.confirmed_molecule", ConfirmedMolecule)
    plan = load(state.prepared_plan_id, "p4.prepared_plan", PreparedPlan)
    if bundle is not None:
        if (
            bundle.run_id != state.run_id
            or bundle.query_id != query.query_id
            or bundle.query_hash != state.query_hash
            or bundle.bundle_hash != state.candidate_bundle_hash
            or bundle.candidate_set_hash != state.candidate_set_hash
        ):
            raise StateIntegrityError("P4 candidate bundle differs from state")
        _source(uow.connection, bundle.record_id, creation.event_id)
        winner = bundle.winning_request
        matches = [
            a
            for a in attempts
            if a.generation == winner.get("generation")
            and a.request_sequence == winner.get("request_sequence")
        ]
        if len(matches) != 1:
            raise StateIntegrityError("P4 winning response attempt is missing or ambiguous")
        attempt = matches[0]
        artifact = ArtifactRecordRepository(uow.connection).get(attempt.response_artifact_id)
        envelope = ResponseEnvelope.model_validate_json(ArtifactStore(state_root).read(artifact))
        if (
            winner.get("effect_id") != str(attempt.effect_id)
            or winner.get("query_hash") != query.query_hash
            or winner.get("response_artifact_id") != str(attempt.response_artifact_id)
            or winner.get("response_envelope_id") != str(envelope.record_id)
            or winner.get("response_body_sha256") != attempt.response_body_sha256
            or winner.get("provider") != attempt.provider.value
        ):
            raise StateIntegrityError("P4 winning request differs from response evidence")
        for candidate in bundle.candidates:
            if (
                candidate.source_artifact_id != attempt.response_artifact_id
                or candidate.source_body_sha256 != attempt.response_body_sha256
                or candidate.provider != attempt.provider
            ):
                raise StateIntegrityError("P4 candidate source binding is invalid")
    if confirmed is not None:
        if bundle is None:
            raise StateIntegrityError("P4 confirmed identity has no candidate bundle")
        matches = [c for c in bundle.candidates if c.candidate_id == confirmed.candidate_id]
        if len(matches) != 1:
            raise StateIntegrityError("P4 confirmed candidate is missing")
        candidate = matches[0]
        if (
            confirmed.run_id != state.run_id
            or confirmed.query_id != query.query_id
            or confirmed.query_hash != query.query_hash
            or confirmed.identity_record_hash != state.confirmed_molecule_hash
            or confirmed.candidate_set_hash != bundle.candidate_set_hash
            or confirmed.candidate_hash != candidate.candidate_hash
            or confirmed.canonical_isomeric_smiles != candidate.canonical_isomeric_smiles
            or confirmed.molecular_formula != candidate.molecular_formula
            or confirmed.formal_charge != query.charge
            or confirmed.formal_charge != candidate.formal_charge
            or confirmed.multiplicity != query.multiplicity
            or confirmed.provider != candidate.provider
            or confirmed.structure_hash
            != sha256_hex(
                {
                    "canonical_isomeric_smiles": candidate.canonical_isomeric_smiles,
                    "formal_charge": candidate.formal_charge,
                    "isotope_labels": list(candidate.isotope_labels),
                    "normalization_strategy": candidate.normalization_strategy,
                    "rdkit_version": candidate.rdkit_version,
                }
            )
        ):
            raise StateIntegrityError("P4 confirmed identity differs from its candidate or state")
        event = uow.events.get(confirmed.confirmation_event_id)
        if (
            event is None
            or event.run_id != state.run_id
            or event.command_id != confirmed.confirmation_command_id
            or event.payload.get("confirmed_molecule_hash") != confirmed.identity_record_hash
            or event.payload.get("confirmed_molecule_id") != str(confirmed.record_id)
        ):
            raise StateIntegrityError("P4 confirmation event binding is invalid")
        _source(uow.connection, confirmed.record_id, event.event_id)
    if plan is not None:
        if confirmed is None or plan.plan_hash != state.prepared_plan_hash:
            raise StateIntegrityError("P4 plan is missing identity or differs from state")
        _source(uow.connection, plan.record_id, confirmed.confirmation_event_id)
        try:
            validate_ground_state_plan(plan, confirmed=confirmed, snapshot=registry)
        except PlanValidationError as error:
            raise StateIntegrityError("P4 persisted plan references are invalid") from error
    return query, registry, bundle, confirmed, plan, attempts


def require_current_normalization(bundle):
    if any(c.normalization_strategy != P4_NORMALIZER_VERSION for c in bundle.candidates):
        raise StateIntegrityError("P4 identity uses an obsolete normalizer; prepare a fresh run")
