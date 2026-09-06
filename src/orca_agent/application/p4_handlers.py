"""Side-effecting P4 identity handlers with a short, fenced write boundary."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from pathlib import Path

from orca_agent.application.errors import StateIntegrityError, StorageError
from orca_agent.application.p4_integrity import verify_record_chain
from orca_agent.domain.canonical import canonical_json_bytes
from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import ArtifactId, WorkflowRecordId
from orca_agent.domain.p4 import (
    CandidateBundle,
    IdentityCandidate,
    IdentityProvider,
    LookupAttempt,
    LookupStatus,
    MoleculeInputKind,
    MoleculeQuery,
    P4Phase,
    P4WorkflowState,
    ResponseEnvelope,
    RetryAfterStatus,
    StereoStatus,
)
from orca_agent.identity.fake_pubchem import FakePubChemAdapter
from orca_agent.identity.ports import (
    IdentityErrorCode,
    LookupResult,
    PubChemPort,
    RawIdentityCandidate,
)
from orca_agent.identity.rdkit_normalizer import IdentityNormalizationError, RDKitNormalizer
from orca_agent.infrastructure.artifacts import ArtifactStore
from orca_agent.infrastructure.clock import Clock, SystemClock
from orca_agent.infrastructure.outbox import DispatchPermit
from orca_agent.infrastructure.p3_records import P4RecordRepository
from orca_agent.infrastructure.sqlite import resolve_database_path
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.infrastructure.worker import HandlerResult
from orca_agent.orchestration.codes import HandlerErrorCode
from orca_agent.orchestration.effect_receipts import EffectSuccessReceiptV1


class P4IdentityHandler:
    """Resolve exactly one P4 identity effect and persist its evidence."""

    max_response_bytes = 2 * 1024 * 1024
    deadline_seconds = 20.0

    def __init__(
        self,
        database_path: str | Path,
        state_root: str | Path,
        *,
        clock: Clock | None = None,
        fake_adapter: PubChemPort | None = None,
        pubchem_adapter: PubChemPort | None = None,
        normalizer: RDKitNormalizer | None = None,
        monotonic=time.monotonic,
    ) -> None:
        self.database_path = resolve_database_path(database_path)
        self.state_root = Path(state_root)
        self.clock = clock or SystemClock()
        self.fake_adapter = fake_adapter or FakePubChemAdapter()
        self.pubchem_adapter = pubchem_adapter
        self.normalizer = normalizer or RDKitNormalizer()
        self._monotonic = monotonic

    def __call__(self, permit: DispatchPermit) -> HandlerResult:
        started = self._monotonic()
        try:
            query, prior_attempt = self._load_query_and_prior_attempt(permit)
            if prior_attempt is not None:
                return self._replay_prior_attempt(prior_attempt)
            if query.input_kind is MoleculeInputKind.SMILES:
                result = self._resolve_local(query)
            else:
                result = self._resolve_provider(query)
            if self._monotonic() - started > self.deadline_seconds:
                result = LookupResult(
                    provider=result.provider,
                    adapter_version=result.adapter_version,
                    body=canonical_json_bytes(
                        {"error_code": IdentityErrorCode.LOOKUP_TIMEOUT.value}
                    ),
                    request_metadata=result.request_metadata,
                    error_code=IdentityErrorCode.LOOKUP_TIMEOUT,
                    retryable=True,
                    status_code=result.status_code,
                    retry_after=result.retry_after,
                )
            return self._persist_result(permit, query, result, started=started)
        except (StateIntegrityError, StorageError):
            raise
        except Exception:
            return HandlerResult(success=False, error_code=HandlerErrorCode.HANDLER_EXCEPTION)

    def _load_query_and_prior_attempt(
        self, permit: DispatchPermit
    ) -> tuple[MoleculeQuery, LookupAttempt | None]:
        with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
            if any(item is None for item in (uow.runs, uow.events, uow.interrupts, uow.outbox)):
                raise StorageError("kernel repositories are unavailable")
            uow.begin()
            uow.outbox.validate_handler_permit(permit=permit, now=self.clock.now_utc())
            snapshot = uow.runs.get_verified(
                permit.effect.run_id,
                uow.events,
                interrupts=uow.interrupts,
                outbox=uow.outbox,
            )
            if not isinstance(snapshot.state, P4WorkflowState):
                raise StateIntegrityError("P4 handler received a non-P4 run")
            if snapshot.state.phase is not P4Phase.RESOLVING_IDENTITY:
                raise StateIntegrityError("P4 identity effect is not in resolving phase")
            verify_record_chain(uow, snapshot.state, self.state_root)
            payload = permit.effect.payload
            if (
                payload.get("run_id") != str(snapshot.run_id)
                or payload.get("query_id") is None
                or payload.get("query_hash") != snapshot.state.query_hash
                or payload.get("effect_id") != str(permit.effect.effect_id)
            ):
                raise StateIntegrityError("P4 identity effect binding is invalid")
            query_id = WorkflowRecordId(str(payload["query_id"]))
            query = P4RecordRepository(uow.connection).get_exact(
                run_id=permit.effect.run_id,
                record_id=query_id,
                record_type="p4.molecule_query",
                model_type=MoleculeQuery,
            )
            if query is None or query.query_hash != snapshot.state.query_hash:
                raise StateIntegrityError("P4 molecule query is missing or unbound")
            prior = P4RecordRepository(uow.connection).attempt_for_generation(
                run_id=permit.effect.run_id,
                effect_id=permit.effect.effect_id,
                generation=permit.generation,
                request_sequence=1,
            )
            uow.commit()
            return query, prior

    def _resolve_provider(self, query: MoleculeQuery) -> LookupResult:
        if query.provider is IdentityProvider.FAKE:
            return self.fake_adapter.resolve(query)
        if query.provider is IdentityProvider.PUBCHEM:
            if self.pubchem_adapter is None:
                from orca_agent.identity.http_pubchem import HttpPubChemAdapter

                adapter = HttpPubChemAdapter(allow_network=False)
            else:
                adapter = self.pubchem_adapter
            return adapter.resolve(query)
        raise StateIntegrityError("P4 provider is invalid for a provider lookup")

    def _resolve_local(self, query: MoleculeQuery) -> LookupResult:
        candidate = RawIdentityCandidate(
            candidate_id=f"local:{hashlib.sha256(query.normalized_input.encode()).hexdigest()[:32]}",
            smiles=query.normalized_input,
            formal_charge=query.charge,
        )
        return LookupResult(
            provider=IdentityProvider.LOCAL,
            adapter_version="local-rdkit-v1",
            body=canonical_json_bytes(
                {
                    "source": "local",
                    "input_kind": query.input_kind.value,
                    "smiles": query.raw_input,
                }
            ),
            request_metadata={
                "adapter": "local-rdkit-v1",
                "input_kind": query.input_kind.value,
                "query_hash": query.query_hash,
            },
            candidates=(candidate,),
        )

    def _persist_result(
        self,
        permit: DispatchPermit,
        query: MoleculeQuery,
        result: LookupResult,
        *,
        started: float | None = None,
    ) -> HandlerResult:
        body = result.body if isinstance(result.body, bytes) else bytes(result.body)
        if not body:
            body = canonical_json_bytes(
                {"error_code": IdentityErrorCode.PROVIDER_SCHEMA_ERROR.value}
            )
            result = self._replace_result_error(
                result, body, IdentityErrorCode.PROVIDER_SCHEMA_ERROR, retryable=False
            )
        if len(body) > self.max_response_bytes:
            body = canonical_json_bytes(
                {"error_code": IdentityErrorCode.RESULT_LIMIT_EXCEEDED.value}
            )
            result = self._replace_result_error(
                result, body, IdentityErrorCode.RESULT_LIMIT_EXCEEDED, retryable=False
            )
        if result.provider is not query.provider and not (
            query.input_kind is MoleculeInputKind.SMILES
            and result.provider is IdentityProvider.LOCAL
        ):
            result = self._replace_result_error(
                result,
                canonical_json_bytes({"error_code": IdentityErrorCode.PROVIDER_SCHEMA_ERROR.value}),
                IdentityErrorCode.PROVIDER_SCHEMA_ERROR,
                retryable=False,
            )
            body = result.body
        candidates: tuple[IdentityCandidate, ...] = ()
        bundle: CandidateBundle | None = None
        error_code = result.error_code
        if result.retryable and error_code is None:
            error_code = IdentityErrorCode.PROVIDER_UNAVAILABLE
        confirmable = False
        reason_codes: tuple[str, ...] = ()
        if result.succeeded:
            try:
                candidates = self._normalize_candidates(query, result)
                if not candidates:
                    raise IdentityNormalizationError(
                        IdentityErrorCode.NOT_FOUND.value, "provider returned no candidates"
                    )
                confirmable, reason_codes = self._protocol_result(query, candidates)
                if not confirmable:
                    error_code = IdentityErrorCode.PROTOCOL_UNSUPPORTED
            except IdentityNormalizationError as error:
                error_code = IdentityErrorCode(error.code)
            except (TypeError, ValueError, KeyError) as error:
                raise StateIntegrityError("P4 provider candidate normalization failed") from error
        if started is not None and self._monotonic() - started >= self.deadline_seconds:
            result = self._replace_result_error(
                result, body, IdentityErrorCode.LOOKUP_TIMEOUT, retryable=True
            )
            error_code = IdentityErrorCode.LOOKUP_TIMEOUT
            candidates = ()
            confirmable = False
        now = self.clock.now_utc()
        with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
            if any(item is None for item in (uow.runs, uow.events, uow.interrupts, uow.outbox)):
                raise StorageError("kernel repositories are unavailable")
            uow.begin()
            uow.outbox.validate_handler_permit(permit=permit, now=now)
            snapshot = uow.runs.get_verified(
                permit.effect.run_id,
                uow.events,
                interrupts=uow.interrupts,
                outbox=uow.outbox,
            )
            if not isinstance(snapshot.state, P4WorkflowState):
                raise StateIntegrityError("P4 result belongs to a non-P4 run")
            records = P4RecordRepository(uow.connection)
            existing = records.attempt_for_generation(
                run_id=permit.effect.run_id,
                effect_id=permit.effect.effect_id,
                generation=permit.generation,
                request_sequence=1,
            )
            if existing is not None:
                uow.commit()
                return self._replay_prior_attempt(existing)
            envelope = ResponseEnvelope.create(
                run_id=query.run_id,
                query_id=query.query_id,
                query_hash=query.query_hash,
                effect_id=permit.effect.effect_id,
                generation=permit.generation,
                request_sequence=1,
                provider=result.provider,
                adapter_version=result.adapter_version,
                request_metadata={
                    **result.request_metadata,
                    "effect_id": str(permit.effect.effect_id),
                    "generation": permit.generation,
                    "request_sequence": 1,
                },
                received_at_utc=now,
                body=body,
            )
            artifact = ArtifactStore(self.state_root, clock=self.clock).put(
                connection=uow.connection,
                run_id=query.run_id,
                content=canonical_json_bytes(envelope.model_dump(mode="json")),
                media_type="application/vnd.orca-agent.p4-response-envelope+json",
            )
            if candidates:
                candidates = tuple(
                    self._bind_candidate_artifact(candidate, artifact.artifact_id)
                    for candidate in candidates
                )
                bundle = CandidateBundle.create(
                    run_id=query.run_id,
                    query_id=query.query_id,
                    query_hash=query.query_hash,
                    candidates=candidates,
                    confirmable=confirmable,
                    reason_codes=reason_codes,
                    winning_request={
                        "provider": result.provider.value,
                        "adapter_version": result.adapter_version,
                        "input_kind": query.input_kind.value,
                        "query": query.normalized_input,
                        "query_hash": query.query_hash,
                        "effect_id": str(permit.effect.effect_id),
                        "generation": permit.generation,
                        "request_sequence": 1,
                        "response_artifact_id": str(artifact.artifact_id),
                        "response_envelope_id": str(envelope.record_id),
                        "response_body_sha256": envelope.body_sha256,
                        "request_metadata": result.request_metadata,
                    },
                )
            retry_status, retry_deadline = self._retry_fields(result)
            status = (
                LookupStatus.RETRYABLE_FAILURE
                if result.retryable and error_code is not None
                else LookupStatus.FAILED
                if error_code is not None
                else LookupStatus.SUCCEEDED
            )
            attempt = LookupAttempt.create(
                run_id=query.run_id,
                query_id=query.query_id,
                query_hash=query.query_hash,
                effect_id=permit.effect.effect_id,
                generation=permit.generation,
                request_sequence=1,
                provider=result.provider,
                adapter_version=result.adapter_version,
                request_summary={
                    "input_kind": query.input_kind.value,
                    "normalized_input": query.normalized_input,
                    "query_hash": query.query_hash,
                    "request_metadata": result.request_metadata,
                },
                received_at_utc=now,
                status=status,
                error_code=None if error_code is None else error_code.value,
                response_artifact_id=artifact.artifact_id,
                response_body_sha256=hashlib.sha256(body).hexdigest(),
                response_envelope_hash=envelope.envelope_hash,
                retry_after_raw=result.retry_after.raw,
                retry_after_status=retry_status,
                retry_not_before_utc=retry_deadline,
            )
            records.append_p4(
                run_id=query.run_id,
                record_type="p4.response_envelope",
                record=envelope,
                created_at_utc=now,
                source_event_id=permit.effect.source_event_id,
                record_id=envelope.record_id,
            )
            records.append_p4(
                run_id=query.run_id,
                record_type="p4.lookup_attempt",
                record=attempt,
                created_at_utc=now,
                source_event_id=permit.effect.source_event_id,
                record_id=attempt.record_id,
            )
            if bundle is not None:
                records.append_p4(
                    run_id=query.run_id,
                    record_type="p4.candidate_bundle",
                    record=bundle,
                    created_at_utc=now,
                    source_event_id=permit.effect.source_event_id,
                    record_id=bundle.record_id,
                )
            uow.commit()
        receipt = EffectSuccessReceiptV1(artifact_ids=(artifact.artifact_id,))
        if status is LookupStatus.RETRYABLE_FAILURE:
            return HandlerResult(
                success=False,
                error_code=HandlerErrorCode.HANDLER_FAILED,
            )
        return HandlerResult(success=True, result_summary=receipt)

    @staticmethod
    def _bind_candidate_artifact(
        candidate: IdentityCandidate, artifact_id: ArtifactId
    ) -> IdentityCandidate:
        values = candidate.model_dump(mode="json", exclude={"candidate_hash"})
        values["source_artifact_id"] = str(artifact_id)
        return IdentityCandidate.model_validate_json(
            json.dumps(
                {
                    **values,
                    "candidate_hash": sha256_hex(values),
                },
                ensure_ascii=False,
            ),
            strict=True,
        )

    def _normalize_candidates(
        self, query: MoleculeQuery, result: LookupResult
    ) -> tuple[IdentityCandidate, ...]:
        if len(result.candidates) > 10:
            raise IdentityNormalizationError(
                IdentityErrorCode.RESULT_LIMIT_EXCEEDED.value,
                "provider returned more than ten candidates",
            )
        values: list[IdentityCandidate] = []
        seen: set[str] = set()
        body_hash = hashlib.sha256(result.body).hexdigest()
        for raw in result.candidates:
            if raw.candidate_id in seen:
                raise IdentityNormalizationError(
                    IdentityErrorCode.PROVIDER_SCHEMA_ERROR.value,
                    "provider returned duplicate candidate IDs",
                )
            seen.add(raw.candidate_id)
            if raw.formal_charge is not None and raw.formal_charge != query.charge:
                raise IdentityNormalizationError(
                    IdentityErrorCode.PROVIDER_SCHEMA_ERROR.value,
                    "candidate charge does not match query",
                )
            normalized = self.normalizer.normalize(
                raw.smiles,
                charge=query.charge,
                multiplicity=query.multiplicity,
            )
            if (
                raw.molecular_formula is not None
                and raw.molecular_formula != normalized.molecular_formula
            ):
                raise IdentityNormalizationError(
                    IdentityErrorCode.PROVIDER_SCHEMA_ERROR.value,
                    "provider formula does not match normalized structure",
                )
            values.append(
                IdentityCandidate.create(
                    candidate_id=raw.candidate_id,
                    source_smiles=normalized.source_smiles,
                    canonical_isomeric_smiles=normalized.canonical_isomeric_smiles,
                    molecular_formula=normalized.molecular_formula,
                    formal_charge=normalized.formal_charge,
                    fragment_count=normalized.fragment_count,
                    radical_electron_count=normalized.radical_electron_count,
                    stereo_status=StereoStatus(normalized.stereo_status),
                    isotope_labels=normalized.isotope_labels,
                    cid=raw.cid,
                    inchikey=raw.inchikey,
                    source_body_sha256=body_hash,
                    provider=result.provider,
                    rdkit_version=normalized.rdkit_version,
                    normalization_strategy=normalized.normalization_strategy,
                    checks=normalized.checks,
                )
            )
        return tuple(values)

    def _protocol_result(
        self, query: MoleculeQuery, candidates: tuple[IdentityCandidate, ...]
    ) -> tuple[bool, tuple[str, ...]]:
        reasons: list[str] = []
        if query.charge != 0:
            reasons.append("non_neutral_charge")
        if query.multiplicity != 1:
            reasons.append("non_singlet_multiplicity")
        for candidate in candidates:
            checks = candidate.checks
            if not checks.get("protocol_range_supported", False):
                reasons.extend(
                    str(item)
                    for item in (
                        "unsupported_elements" if checks.get("unsupported_elements") else None,
                        "multiple_fragments" if candidate.fragment_count != 1 else None,
                        "radical_structure" if candidate.radical_electron_count else None,
                        "unspecified_stereo"
                        if candidate.stereo_status is StereoStatus.UNSPECIFIED
                        else None,
                        "non_h_atom_limit" if checks.get("non_h_atom_count", 0) > 50 else None,
                    )
                    if item is not None
                )
        return not reasons, tuple(dict.fromkeys(reasons))

    @staticmethod
    def _retry_fields(result: LookupResult) -> tuple[RetryAfterStatus, datetime | None]:
        raw_status = result.retry_after.status
        try:
            status = RetryAfterStatus(raw_status)
        except ValueError:
            status = RetryAfterStatus.INVALID
        deadline = result.retry_after.not_before_utc if status is RetryAfterStatus.VALID else None
        return status, deadline

    @staticmethod
    def _replace_result_error(
        result: LookupResult,
        body: bytes,
        code: IdentityErrorCode,
        *,
        retryable: bool,
    ) -> LookupResult:
        return LookupResult(
            provider=result.provider,
            adapter_version=result.adapter_version,
            body=body,
            request_metadata=result.request_metadata,
            candidates=(),
            error_code=code,
            retryable=retryable,
            status_code=result.status_code,
            retry_after=result.retry_after,
        )

    @staticmethod
    def _replay_prior_attempt(attempt: LookupAttempt) -> HandlerResult:
        if attempt.status is LookupStatus.RETRYABLE_FAILURE:
            return HandlerResult(success=False, error_code=HandlerErrorCode.HANDLER_FAILED)
        if attempt.response_artifact_id is None:
            raise StateIntegrityError("P4 prior attempt is missing its response artifact")
        return HandlerResult(
            success=True,
            result_summary=EffectSuccessReceiptV1(artifact_ids=(attempt.response_artifact_id,)),
        )


__all__ = ["P4IdentityHandler"]
