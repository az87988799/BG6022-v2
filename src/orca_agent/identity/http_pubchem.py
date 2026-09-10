"""Explicit live PubChem adapter.

This is the only P4 module that imports HTTPX.  It returns typed raw data and
never persists records or performs retry loops; the Worker owns retries.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from orca_agent.domain.p4 import IdentityProvider, MoleculeInputKind, MoleculeQuery

from .ports import (
    IdentityErrorCode,
    IdentityLookupRequest,
    LookupResult,
    PubChemPort,
    RawIdentityCandidate,
    RetryAfter,
)


class HttpPubChemAdapter(PubChemPort):
    """PUG REST client with a fixed HTTPS origin and bounded responses."""

    adapter_version = "pubchem-http-v1"
    base_url = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
    max_response_bytes = 2 * 1024 * 1024

    def __init__(
        self,
        *,
        allow_network: bool = False,
        timeout_seconds: float = 5.0,
        transport: object | None = None,
        monotonic=time.monotonic,
        sleeper=time.sleep,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if allow_network:
            try:
                import httpx  # noqa: F401
            except ModuleNotFoundError as error:
                raise RuntimeError(
                    "P4 live PubChem requires the p4 optional dependencies"
                ) from error
        self.allow_network = allow_network
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._last_request_at: float | None = None

    def resolve(self, query: MoleculeQuery) -> LookupResult:
        return self._lookup(
            input_kind=query.input_kind,
            normalized_input=query.normalized_input,
            requested_charge=query.charge,
        )

    def lookup_candidates(self, request: IdentityLookupRequest) -> LookupResult:
        return self._lookup(
            input_kind=request.input_kind,
            normalized_input=request.normalized_value,
            requested_charge=None,
        )

    def _lookup(
        self,
        *,
        input_kind: MoleculeInputKind,
        normalized_input: str,
        requested_charge: int | None,
    ) -> LookupResult:
        if not self.allow_network:
            return self._failure(
                input_kind=input_kind,
                normalized_input=normalized_input,
                code=IdentityErrorCode.NETWORK_DISABLED,
                retryable=False,
                body={"error_code": IdentityErrorCode.NETWORK_DISABLED.value},
            )
        if input_kind is MoleculeInputKind.SMILES:
            return self._failure(
                input_kind=input_kind,
                normalized_input=normalized_input,
                code=IdentityErrorCode.QUERY_REJECTED,
                retryable=False,
                body={"error_code": IdentityErrorCode.QUERY_REJECTED.value},
            )
        import httpx

        deadline = self._monotonic() + 20.0
        path_kind = "cid" if input_kind is MoleculeInputKind.CID else "name"
        encoded = quote(normalized_input, safe="")
        url = (
            f"{self.base_url}/compound/{path_kind}/{encoded}/property/"
            "SMILES,ConnectivitySMILES,InChI,InChIKey,MolecularFormula,Charge/JSON"
        )
        if input_kind in (MoleculeInputKind.NAME, MoleculeInputKind.CAS):
            url += "?name_type=complete"
        try:
            self._respect_rate_limit()
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise TimeoutError("PubChem deadline exceeded before request")
            with httpx.Client(
                base_url=self.base_url,
                timeout=httpx.Timeout(min(self.timeout_seconds, remaining)),
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                with client.stream("GET", url.removeprefix(self.base_url)) as response:
                    body = self._read_bounded(response, deadline=deadline)
        except (httpx.TimeoutException, TimeoutError):
            return self._failure(
                input_kind=input_kind,
                normalized_input=normalized_input,
                code=IdentityErrorCode.LOOKUP_TIMEOUT,
                retryable=True,
                body={"error_code": IdentityErrorCode.LOOKUP_TIMEOUT.value},
            )
        except (httpx.NetworkError, httpx.TransportError):
            return self._failure(
                input_kind=input_kind,
                normalized_input=normalized_input,
                code=IdentityErrorCode.PROVIDER_UNAVAILABLE,
                retryable=True,
                body={"error_code": IdentityErrorCode.PROVIDER_UNAVAILABLE.value},
            )
        except ValueError:
            return self._failure(
                input_kind=input_kind,
                normalized_input=normalized_input,
                code=IdentityErrorCode.RESULT_LIMIT_EXCEEDED,
                retryable=False,
                body={"error_code": IdentityErrorCode.RESULT_LIMIT_EXCEEDED.value},
            )

        status = response.status_code
        retry_after = _parse_retry_after(response.headers.get("Retry-After"), datetime.now(UTC))
        if status == 429 or status == 503:
            return self._result_error(
                input_kind,
                normalized_input,
                body,
                status,
                IdentityErrorCode.PROVIDER_THROTTLED,
                retryable=True,
                retry_after=retry_after,
            )
        if status in (502, 504):
            return self._result_error(
                input_kind,
                normalized_input,
                body,
                status,
                IdentityErrorCode.PROVIDER_UNAVAILABLE,
                retryable=True,
                retry_after=retry_after,
            )
        if status == 400:
            return self._result_error(
                input_kind, normalized_input, body, status, IdentityErrorCode.QUERY_REJECTED
            )
        if status == 404:
            return self._result_error(
                input_kind, normalized_input, body, status, IdentityErrorCode.NOT_FOUND
            )
        if status < 200 or status >= 300:
            return self._result_error(
                input_kind,
                normalized_input,
                body,
                status,
                IdentityErrorCode.PROVIDER_UNAVAILABLE,
                retryable=True,
            )
        try:
            document = json.loads(body.decode("utf-8"))
            candidates = _parse_candidates(
                document,
                input_kind=input_kind,
                normalized_input=normalized_input,
                requested_charge=requested_charge,
            )
        except (UnicodeDecodeError, ValueError, TypeError, KeyError):
            return self._result_error(
                input_kind, normalized_input, body, status, IdentityErrorCode.PROVIDER_SCHEMA_ERROR
            )
        if not candidates:
            return self._result_error(
                input_kind, normalized_input, body, status, IdentityErrorCode.NOT_FOUND
            )
        if len(candidates) > 10:
            return self._result_error(
                input_kind, normalized_input, body, status, IdentityErrorCode.RESULT_LIMIT_EXCEEDED
            )
        return LookupResult(
            provider=IdentityProvider.PUBCHEM,
            adapter_version=self.adapter_version,
            body=body,
            request_metadata={
                "host": "pubchem.ncbi.nlm.nih.gov",
                "path_kind": path_kind,
                "input_kind": input_kind.value,
                "query": normalized_input,
                "q_m_filter": requested_charge is not None,
            },
            candidates=tuple(candidates),
            status_code=status,
            retry_after=retry_after,
        )

    def _respect_rate_limit(self) -> None:
        if self._last_request_at is not None:
            remaining = 1.0 - (self._monotonic() - self._last_request_at)
            if remaining > 0:
                self._sleeper(remaining)
        self._last_request_at = self._monotonic()

    def _read_bounded(self, response: object, *, deadline: float) -> bytes:
        body = bytearray()
        for chunk in response.iter_bytes():
            if self._monotonic() >= deadline:
                raise TimeoutError("PubChem response deadline exceeded")
            if len(body) + len(chunk) > self.max_response_bytes:
                raise ValueError("PubChem response exceeds 2 MiB")
            body.extend(chunk)
        if self._monotonic() >= deadline:
            raise TimeoutError("PubChem response deadline exceeded")
        return bytes(body)

    def _result_error(
        self,
        input_kind: MoleculeInputKind,
        normalized_input: str,
        body: bytes,
        status: int,
        code: IdentityErrorCode,
        *,
        retryable: bool = False,
        retry_after: RetryAfter | None = None,
    ) -> LookupResult:
        return LookupResult(
            provider=IdentityProvider.PUBCHEM,
            adapter_version=self.adapter_version,
            body=body,
            request_metadata={
                "host": "pubchem.ncbi.nlm.nih.gov",
                "status_code": status,
                "input_kind": input_kind.value,
                "query": normalized_input,
            },
            error_code=code,
            retryable=retryable,
            status_code=status,
            retry_after=retry_after or RetryAfter(raw=None, status="missing"),
        )

    def _failure(
        self,
        *,
        input_kind: MoleculeInputKind,
        normalized_input: str,
        code: IdentityErrorCode,
        retryable: bool,
        body: dict[str, object],
    ) -> LookupResult:
        return LookupResult(
            provider=IdentityProvider.PUBCHEM,
            adapter_version=self.adapter_version,
            body=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            request_metadata={
                "host": "pubchem.ncbi.nlm.nih.gov",
                "input_kind": input_kind.value,
                "query": normalized_input,
            },
            error_code=code,
            retryable=retryable,
            retry_after=RetryAfter(raw=None, status="missing"),
        )


def _parse_candidates(
    document: object,
    *,
    input_kind: MoleculeInputKind,
    normalized_input: str,
    requested_charge: int | None,
) -> list[RawIdentityCandidate]:
    if not isinstance(document, dict):
        raise ValueError("provider response is not an object")
    table = document.get("PropertyTable")
    if not isinstance(table, dict) or not isinstance(table.get("Properties"), list):
        raise ValueError("provider response has no PropertyTable.Properties")
    values: list[RawIdentityCandidate] = []
    requested_cid = int(normalized_input) if input_kind is MoleculeInputKind.CID else None
    for item in table["Properties"]:
        if not isinstance(item, dict):
            raise ValueError("provider candidate is not an object")
        cid = item.get("CID")
        smiles = item.get("SMILES")
        if type(cid) is not int or cid <= 0 or not isinstance(smiles, str) or not smiles:
            raise ValueError("provider candidate is missing CID or SMILES")
        if requested_cid is not None and cid != requested_cid:
            raise ValueError("CID response does not match query")
        formal_charge = item.get("Charge")
        if type(formal_charge) is not int:
            raise ValueError("provider candidate charge is missing or invalid")
        if requested_charge is not None and formal_charge != requested_charge:
            raise ValueError("provider candidate charge does not match query")
        values.append(
            RawIdentityCandidate(
                candidate_id=f"cid:{cid}",
                cid=cid,
                smiles=smiles,
                inchikey=_optional_text(item.get("InChIKey")),
                molecular_formula=_optional_text(item.get("MolecularFormula")),
                connectivity_smiles=_optional_text(item.get("ConnectivitySMILES")),
                inchi=_optional_text(item.get("InChI")),
                formal_charge=formal_charge,
            )
        )
    return values


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse_retry_after(raw: str | None, now: datetime) -> RetryAfter:
    if raw is None:
        return RetryAfter(raw=None, status="missing")
    value = raw.strip()
    if not value:
        return RetryAfter(raw=raw, status="invalid")
    try:
        seconds = int(value)
        if seconds < 0:
            raise ValueError
        return RetryAfter(raw=raw, status="valid", not_before_utc=now + timedelta(seconds=seconds))
    except (ValueError, OverflowError):
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                raise ValueError
            return RetryAfter(raw=raw, status="valid", not_before_utc=deadline.astimezone(UTC))
        except (TypeError, ValueError, OverflowError):
            return RetryAfter(raw=raw, status="invalid")


__all__ = ["HttpPubChemAdapter"]
