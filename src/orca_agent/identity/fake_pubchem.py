"""Deterministic PubChem-shaped fixture adapter for offline P4 tests."""

from __future__ import annotations

from collections.abc import Mapping

from orca_agent.domain.canonical import canonical_json_bytes
from orca_agent.domain.p4 import IdentityProvider, MoleculeInputKind, MoleculeQuery

from .ports import (
    IdentityErrorCode,
    IdentityLookupRequest,
    LookupResult,
    PubChemPort,
    RawIdentityCandidate,
    RetryAfter,
)


class FakePubChemAdapter(PubChemPort):
    """A fixed, explicit fixture map; it never opens a socket."""

    adapter_version = "fake-pubchem-v1"

    _DEFAULTS: Mapping[str, tuple[RawIdentityCandidate, ...]] = {
        "name:water": (
            RawIdentityCandidate(
                candidate_id="cid:962",
                cid=962,
                smiles="O",
                inchikey="XLYOFNOQVPJJNP-UHFFFAOYSA-N",
                molecular_formula="H2O",
            ),
        ),
        "name:ethanol": (
            RawIdentityCandidate(
                candidate_id="cid:702",
                cid=702,
                smiles="CCO",
                inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
                molecular_formula="C2H6O",
            ),
        ),
        "name:benzene": (
            RawIdentityCandidate(
                candidate_id="cid:241",
                cid=241,
                smiles="c1ccccc1",
                inchikey="UHOVQNZJYSORNB-UHFFFAOYSA-N",
                molecular_formula="C6H6",
            ),
        ),
        "cas:7732-18-5": (
            RawIdentityCandidate(
                candidate_id="cid:962",
                cid=962,
                smiles="O",
                inchikey="XLYOFNOQVPJJNP-UHFFFAOYSA-N",
                molecular_formula="H2O",
            ),
        ),
        "cas:64-17-5": (
            RawIdentityCandidate(
                candidate_id="cid:702",
                cid=702,
                smiles="CCO",
                inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
                molecular_formula="C2H6O",
            ),
        ),
        "cas:71-43-2": (
            RawIdentityCandidate(
                candidate_id="cid:241",
                cid=241,
                smiles="c1ccccc1",
                inchikey="UHOVQNZJYSORNB-UHFFFAOYSA-N",
                molecular_formula="C6H6",
            ),
        ),
        "cid:962": (
            RawIdentityCandidate(
                candidate_id="cid:962",
                cid=962,
                smiles="O",
                inchikey="XLYOFNOQVPJJNP-UHFFFAOYSA-N",
                molecular_formula="H2O",
            ),
        ),
        "cid:702": (
            RawIdentityCandidate(
                candidate_id="cid:702",
                cid=702,
                smiles="CCO",
                inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
                molecular_formula="C2H6O",
            ),
        ),
        "cid:241": (
            RawIdentityCandidate(
                candidate_id="cid:241",
                cid=241,
                smiles="c1ccccc1",
                inchikey="UHOVQNZJYSORNB-UHFFFAOYSA-N",
                molecular_formula="C6H6",
            ),
        ),
    }

    def __init__(
        self,
        records: Mapping[str, tuple[RawIdentityCandidate, ...]] | None = None,
        *,
        adapter_version: str | None = None,
    ) -> None:
        self.records = dict(self._DEFAULTS if records is None else records)
        if adapter_version is not None:
            self.adapter_version = adapter_version

    def resolve(self, query: MoleculeQuery) -> LookupResult:
        key = f"{query.input_kind.value}:{query.normalized_input.casefold()}"
        candidates = self.records.get(key, ())
        if query.input_kind is MoleculeInputKind.CID and candidates:
            if any(candidate.cid != int(query.normalized_input) for candidate in candidates):
                return self._failure(
                    query,
                    IdentityErrorCode.PROVIDER_SCHEMA_ERROR,
                    "CID response does not match query",
                )
        if not candidates:
            return self._failure(query, IdentityErrorCode.NOT_FOUND, "fixture has no matching CID")
        document = {
            "PropertyTable": {
                "Properties": [
                    {
                        "CID": candidate.cid,
                        "SMILES": candidate.smiles,
                        "ConnectivitySMILES": candidate.connectivity_smiles,
                        "InChI": candidate.inchi,
                        "InChIKey": candidate.inchikey,
                        "MolecularFormula": candidate.molecular_formula,
                        "Charge": query.charge,
                    }
                    for candidate in candidates
                ]
            }
        }
        return LookupResult(
            provider=IdentityProvider.FAKE,
            adapter_version=self.adapter_version,
            body=canonical_json_bytes(document),
            request_metadata={
                "fixture": "pubchem-shaped-v1",
                "input_kind": query.input_kind.value,
                "query": query.normalized_input,
            },
            candidates=candidates,
        )

    def lookup_candidates(self, request: IdentityLookupRequest) -> LookupResult:
        """Lookup fixture properties without inventing a q/M constraint."""

        key = f"{request.input_kind.value}:{request.normalized_value.casefold()}"
        candidates = self.records.get(key, ())
        if not candidates:
            return LookupResult(
                provider=IdentityProvider.FAKE,
                adapter_version=self.adapter_version,
                body=canonical_json_bytes(
                    {
                        "error_code": IdentityErrorCode.NOT_FOUND.value,
                        "message": "fixture has no matching identity",
                        "query": request.normalized_value,
                    }
                ),
                request_metadata={
                    "fixture": "pubchem-shaped-v1",
                    "input_kind": request.input_kind.value,
                    "query": request.normalized_value,
                },
                error_code=IdentityErrorCode.NOT_FOUND,
            )
        document = {
            "PropertyTable": {
                "Properties": [
                    {
                        "CID": candidate.cid,
                        "SMILES": candidate.smiles,
                        "ConnectivitySMILES": candidate.connectivity_smiles,
                        "InChI": candidate.inchi,
                        "InChIKey": candidate.inchikey,
                        "MolecularFormula": candidate.molecular_formula,
                        "Charge": candidate.formal_charge,
                    }
                    for candidate in candidates
                ]
            }
        }
        return LookupResult(
            provider=IdentityProvider.FAKE,
            adapter_version=self.adapter_version,
            body=canonical_json_bytes(document),
            request_metadata={
                "fixture": "pubchem-shaped-v1",
                "input_kind": request.input_kind.value,
                "query": request.normalized_value,
                "q_m_not_used": True,
            },
            candidates=candidates,
        )

    def _failure(self, query: MoleculeQuery, code: IdentityErrorCode, message: str) -> LookupResult:
        return LookupResult(
            provider=IdentityProvider.FAKE,
            adapter_version=self.adapter_version,
            body=canonical_json_bytes(
                {"error_code": code.value, "message": message, "query": query.normalized_input}
            ),
            request_metadata={
                "fixture": "pubchem-shaped-v1",
                "input_kind": query.input_kind.value,
                "query": query.normalized_input,
            },
            error_code=code,
            retryable=False,
            retry_after=RetryAfter(raw=None, status="missing"),
        )


__all__ = ["FakePubChemAdapter"]
