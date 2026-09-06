"""Independent regression cases from the dbb36f0 P4 audit."""

import json
import sqlite3
from datetime import timedelta

import httpx
import pytest
from pydantic import ValidationError
from test_identity_workflow import (
    BASE_TIME,
    _confirm,
    _pubchem_query,
    _resolve,
    _service,
    _start,
)

from orca_agent.application.errors import StateIntegrityError
from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.p4 import PreparedPlan
from orca_agent.domain.registry import MethodRegistryEntry, RegistrySnapshot
from orca_agent.identity.http_pubchem import HttpPubChemAdapter, _parse_retry_after
from orca_agent.identity.ports import IdentityErrorCode, LookupResult, RetryAfter
from orca_agent.identity.rdkit_normalizer import RDKitNormalizer
from orca_agent.planning.validator import PlanValidationError, validate_ground_state_plan


@pytest.mark.parametrize(
    "smiles,electrons",
    [
        ("N", 10),
        ("CN", 18),
        ("FC", 18),
        ("ClC", 26),
        ("O", 10),
        ("CCO", 26),
        ("c1ccccc1", 42),
        ("[2H]O[2H]", 10),
        ("[H]N([H])[H]", 10),
    ],
)
def test_all_hydrogens_counted_once(smiles, electrons):
    result = RDKitNormalizer().normalize(smiles, charge=0, multiplicity=1)
    assert result.checks["electron_count"] == electrons
    assert result.normalization_strategy == "rdkit-normalizer-v2"


def test_explicit_implicit_hydrogen_identity_equivalence():
    normalizer = RDKitNormalizer()
    assert normalizer.normalize("N", charge=0, multiplicity=1).structure_hash == (
        normalizer.normalize("[H]N([H])[H]", charge=0, multiplicity=1).structure_hash
    )


@pytest.mark.parametrize("suffix", [" |o1:1|", " |&1:1|", " |$foo$|", " name"])
def test_cx_identity_features_are_rejected(suffix):
    with pytest.raises(ValueError, match="unsupported"):
        RDKitNormalizer().normalize("C[C@H](F)Cl" + suffix, charge=0, multiplicity=1)


def test_original_prepare_response_replays_after_each_transition(tmp_path):
    service, clock = _service(tmp_path)
    command, original = _start(service, clock)
    view = _resolve(service, original)
    assert service.start(command) == original
    _, result = _confirm(service, clock, view)
    assert result.accepted
    assert service.start(command) == original


def _ready(tmp_path):
    service, clock = _service(tmp_path)
    _, started = _start(service, clock)
    view = _resolve(service, started)
    _, result = _confirm(service, clock, view)
    assert result.accepted
    return service, service.inspect(started.run_id)


def _remove_protection(connection):
    # Fault injection is confined to this test's fresh temporary database.
    names = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='workflow_records'"
    ).fetchall()
    for (name,) in names:
        connection.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')


@pytest.mark.parametrize(
    "kind",
    ["p4.confirmed_molecule", "p4.candidate_bundle", "p4.response_envelope", "p4.lookup_attempt"],
)
def test_missing_source_chain_blocks_inspect_and_export(tmp_path, kind):
    service, view = _ready(tmp_path)
    with sqlite3.connect(service.database_path) as connection:
        _remove_protection(connection)
        connection.execute("DELETE FROM workflow_records WHERE record_type=?", (kind,))
    with pytest.raises(StateIntegrityError):
        service.inspect(view.run_id)
    with pytest.raises(StateIntegrityError):
        service.export_plan(view.run_id)


@pytest.mark.parametrize(
    "field,value",
    [
        ("protocol_id", "missing"),
        ("method_profile_id", "missing"),
        ("protocol_hash", "0" * 64),
        ("method_profile_hash", "0" * 64),
    ],
)
def test_validly_hashed_plan_references_are_closed(tmp_path, field, value):
    _, view = _ready(tmp_path)
    data = view.prepared_plan.model_dump(mode="json")
    data[field] = value
    data["plan_hash"] = sha256_hex({k: v for k, v in data.items() if k != "plan_hash"})
    plan = PreparedPlan.model_validate_json(json.dumps(data))
    with pytest.raises(PlanValidationError):
        validate_ground_state_plan(plan, confirmed=view.confirmed_molecule, snapshot=view.registry)


def test_locally_rehashed_plan_cannot_diverge_from_event(tmp_path):
    service, view = _ready(tmp_path)
    data = view.prepared_plan.model_dump(mode="json")
    data["proposal"]["rationale"] = "tampered rationale"
    data["plan_hash"] = sha256_hex({k: v for k, v in data.items() if k != "plan_hash"})
    plan = PreparedPlan.model_validate_json(json.dumps(data))
    from orca_agent.domain.canonical import canonical_json_bytes

    with sqlite3.connect(service.database_path) as connection:
        _remove_protection(connection)
        connection.execute(
            "UPDATE workflow_records SET record_json=?,record_hash=? WHERE record_id=?",
            (canonical_json_bytes(plan).decode(), sha256_hex(plan), str(plan.record_id)),
        )
    with pytest.raises(StateIntegrityError, match="differs from state"):
        service.export_plan(view.run_id)


def test_registry_duplicate_identity_and_forged_manifest_fail(tmp_path):
    _, view = _ready(tmp_path)
    snapshot = view.registry
    method = snapshot.methods[0].model_dump(mode="json")
    method["method_name"] = "B3LYP"
    method["entry_hash"] = sha256_hex({k: v for k, v in method.items() if k != "entry_hash"})
    conflicting = MethodRegistryEntry.model_validate_json(json.dumps(method))
    with pytest.raises(ValidationError, match="duplicated"):
        RegistrySnapshot.create(
            registry_version=snapshot.registry_version,
            methods=(*snapshot.methods, conflicting),
            primitives=snapshot.primitives,
            protocols=snapshot.protocols,
            capabilities=snapshot.capabilities,
        )
    data = snapshot.model_dump(mode="json")
    data["manifest"][0]["entry_hash"] = "0" * 64
    data["manifest_hash"] = sha256_hex(data["manifest"])
    data["snapshot_hash"] = sha256_hex({k: v for k, v in data.items() if k != "snapshot_hash"})
    with pytest.raises(ValidationError, match="actual entries"):
        RegistrySnapshot.model_validate_json(json.dumps(data))


def test_stream_limit_stops_consuming_response():
    consumed = []

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            for index in range(8):
                consumed.append(index)
                yield b"x" * (512 * 1024)

    adapter = HttpPubChemAdapter(
        allow_network=True,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream())),
    )
    assert adapter.resolve(_pubchem_query()).error_code is IdentityErrorCode.RESULT_LIMIT_EXCEEDED
    assert len(consumed) == 5


@pytest.mark.parametrize("raw", ["", " ", "9" * 300, "-1", "not-a-date"])
def test_invalid_retry_headers_persist_and_use_backoff(tmp_path, raw):
    class Adapter:
        def resolve(self, query):
            return LookupResult(
                provider=query.provider,
                adapter_version="audit",
                body=b"throttled",
                request_metadata={},
                error_code=IdentityErrorCode.PROVIDER_THROTTLED,
                retryable=True,
                retry_after=_parse_retry_after(raw, BASE_TIME),
            )

    service, clock = _service(tmp_path, adapter=Adapter())
    _, started = _start(service, clock)
    assert service.create_worker().run_once(limit=1)[0].outcome == "retry"
    view = service.inspect(started.run_id)
    assert view.attempts[0].retry_after_raw == raw
    assert view.attempts[0].retry_after_status.value == "invalid"
    assert service.create_worker().run_once(limit=1) == ()


def test_corrupt_retry_evidence_blocks_provider_before_claim(tmp_path):
    class Adapter:
        calls = 0

        def resolve(self, query):
            self.calls += 1
            return LookupResult(
                provider=query.provider,
                adapter_version="audit",
                body=b"throttled",
                request_metadata={},
                error_code=IdentityErrorCode.PROVIDER_THROTTLED,
                retryable=True,
                retry_after=RetryAfter(
                    raw="5", status="valid", not_before_utc=BASE_TIME + timedelta(seconds=5)
                ),
            )

    adapter = Adapter()
    service, clock = _service(tmp_path, adapter=adapter)
    _, started = _start(service, clock)
    service.create_worker().run_once(limit=1)
    view = service.inspect(started.run_id)
    with sqlite3.connect(service.database_path) as connection:
        path = connection.execute(
            "SELECT relative_path FROM artifacts WHERE artifact_id=?",
            (str(view.attempts[0].response_artifact_id),),
        ).fetchone()[0]
    artifact = service.state_root / "artifacts" / path
    artifact.write_bytes(artifact.read_bytes() + b"corrupt")
    clock.advance(timedelta(seconds=5))
    with pytest.raises(StateIntegrityError):
        service.create_worker().run_once(limit=1)
    assert adapter.calls == 1


def test_slow_normalizer_cannot_produce_success(tmp_path):
    service, clock = _service(tmp_path)
    _, started = _start(service, clock)
    ticks = [0.0]
    real = RDKitNormalizer()

    class SlowNormalizer:
        def normalize(self, *args, **kwargs):
            result = real.normalize(*args, **kwargs)
            ticks[0] += 25
            return result

    worker = service.create_worker()
    worker.handler.normalizer = SlowNormalizer()
    worker.handler._monotonic = lambda: ticks[0]
    assert worker.run_once(limit=1)[0].outcome == "retry"
    view = service.inspect(started.run_id)
    assert view.attempts[0].error_code == "identity_lookup_timeout"
    assert view.candidate_bundle is None


def test_rate_limit_deadline_prevents_request():
    ticks = [0.0]
    calls = []

    def sleep(_seconds):
        ticks[0] += 25

    adapter = HttpPubChemAdapter(
        allow_network=True,
        monotonic=lambda: ticks[0],
        sleeper=sleep,
        transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(404)),
    )
    adapter.resolve(_pubchem_query())
    assert adapter.resolve(_pubchem_query()).error_code is IdentityErrorCode.LOOKUP_TIMEOUT
    assert len(calls) == 1


@pytest.mark.parametrize("field", ["problem_spec_id", "problem_spec_hash"])
def test_proposal_problem_reference_must_match(tmp_path, field):
    _, view = _ready(tmp_path)
    data = view.prepared_plan.model_dump(mode="json")
    data["proposal"][field] = "problem_" + "0" * 32 if field == "problem_spec_id" else "0" * 64
    data["plan_hash"] = sha256_hex({k: v for k, v in data.items() if k != "plan_hash"})
    plan = PreparedPlan.model_validate_json(json.dumps(data))
    with pytest.raises(PlanValidationError, match="problem binding"):
        validate_ground_state_plan(plan, confirmed=view.confirmed_molecule, snapshot=view.registry)


def test_obsolete_normalizer_is_readable_but_not_confirmable(tmp_path):
    service, clock = _service(tmp_path)
    _, started = _start(service, clock)
    worker = service.create_worker()

    class PreviousNormalizer(RDKitNormalizer):
        strategy_version = "rdkit-normalizer-v1"

    worker.handler.normalizer = PreviousNormalizer()
    worker.run_once(limit=1)
    view = service.inspect(started.run_id)
    assert view.candidate_bundle.candidates[0].normalization_strategy == "rdkit-normalizer-v1"
    _, result = _confirm(service, clock, view)
    assert not result.accepted and result.code == "state_integrity_error"


@pytest.mark.parametrize("delete_attempt", [False, True])
def test_committed_retry_pair_survives_completion_crash(tmp_path, delete_attempt):
    class Adapter:
        calls = 0

        def resolve(self, query):
            self.calls += 1
            return LookupResult(
                provider=query.provider,
                adapter_version="reaudit",
                body=b"throttled",
                request_metadata={},
                error_code=IdentityErrorCode.PROVIDER_THROTTLED,
                retryable=True,
                retry_after=RetryAfter(
                    raw="10", status="valid", not_before_utc=BASE_TIME + timedelta(seconds=10)
                ),
            )

    class Crash(BaseException):
        pass

    adapter = Adapter()
    service, clock = _service(tmp_path, adapter=adapter)
    _, started = _start(service, clock)
    worker = service.create_worker(lease_duration=timedelta(seconds=1))
    original = worker.handler

    def crash_after_commit(permit):
        original(permit)
        raise Crash()

    worker.handler = crash_after_commit
    with pytest.raises(Crash):
        worker.run_once(limit=1)
    if delete_attempt:
        with sqlite3.connect(service.database_path) as connection:
            _remove_protection(connection)
            connection.execute("DELETE FROM workflow_records WHERE record_type='p4.lookup_attempt'")
    clock.advance(timedelta(seconds=2))
    recovered = service.create_worker(lease_duration=timedelta(seconds=1))
    if delete_attempt:
        with pytest.raises(StateIntegrityError, match="paired"):
            service.inspect(started.run_id)
        with pytest.raises(StateIntegrityError, match="paired"):
            recovered.run_once(limit=1)
    else:
        assert len(service.inspect(started.run_id).attempts) == 1
        assert recovered.run_once(limit=1) == ()
    assert adapter.calls == 1
    with sqlite3.connect(service.database_path) as connection:
        assert connection.execute("SELECT attempt_count FROM outbox").fetchone()[0] == 1


def test_crash_before_either_source_record_allows_recovery(tmp_path):
    class Crash(BaseException):
        pass

    service, clock = _service(tmp_path)
    _, started = _start(service, clock)
    worker = service.create_worker(lease_duration=timedelta(seconds=1))

    def crash_before_handler(_permit):
        raise Crash()

    worker.handler = crash_before_handler
    with pytest.raises(Crash):
        worker.run_once(limit=1)
    assert service.inspect(started.run_id).attempts == ()
    clock.advance(timedelta(seconds=2))
    reports = service.create_worker(lease_duration=timedelta(seconds=1)).run_once(limit=1)
    assert reports[0].outcome == "succeeded" and reports[0].attempt_count == 2
    assert service.inspect(started.run_id).state.phase.value == "awaiting_identity"


@pytest.mark.parametrize("fault", ["duplicate", "wrong_query", "wrong_envelope_id"])
def test_response_pair_rejects_duplicate_and_mismatched_records(tmp_path, fault):
    from orca_agent.domain.canonical import canonical_json_bytes
    from orca_agent.domain.ids import WorkflowRecordId, new_id
    from orca_agent.domain.p4 import ResponseEnvelope
    from orca_agent.infrastructure.p3_records import P4RecordRepository
    from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork

    service, clock = _service(tmp_path)
    _, started = _start(service, clock)
    _resolve(service, started)
    with SQLiteUnitOfWork(service.database_path, clock=clock) as uow:
        records = P4RecordRepository(uow.connection)
        envelope = next(
            value
            for _, kind, value in records.list_for_run(started.run_id)
            if kind == "p4.response_envelope"
        )
        data = envelope.model_dump(mode="json")
        if fault in ("duplicate", "wrong_envelope_id"):
            data["record_id"] = str(new_id(WorkflowRecordId))
        else:
            data["query_id"] = str(new_id(WorkflowRecordId))
        data["envelope_hash"] = sha256_hex({k: v for k, v in data.items() if k != "envelope_hash"})
        changed = ResponseEnvelope.model_validate_json(json.dumps(data))
        source = uow.connection.execute(
            "SELECT source_event_id FROM workflow_records WHERE record_id=?",
            (str(envelope.record_id),),
        ).fetchone()[0]
        _remove_protection(uow.connection)
        if fault != "duplicate":
            uow.connection.execute(
                "DELETE FROM workflow_records WHERE record_id=?", (str(envelope.record_id),)
            )
        records.append_p4(
            run_id=started.run_id,
            record_type="p4.response_envelope",
            record=changed,
            record_id=changed.record_id,
            source_event_id=source,
            created_at_utc=clock.now_utc(),
        )
        assert canonical_json_bytes(changed)
        uow.commit()
    with pytest.raises(StateIntegrityError):
        service.inspect(started.run_id)
