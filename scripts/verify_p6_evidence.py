"""Verify a P6 evidence export against its still-available local ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from orca_agent.application.p6_service import P6ApplicationService
from orca_agent.domain.canonical import canonical_json_bytes
from orca_agent.domain.ids import RunId
from orca_agent.domain.p6 import P6ReportManifest
from orca_agent.infrastructure.artifacts import ArtifactStore
from orca_agent.infrastructure.p3_records import ArtifactRecordRepository
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.reporting.p6_renderer import P6ReportRenderer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--run-id", type=RunId, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.evidence_root.resolve()
    service = P6ApplicationService(args.state_root)
    verification = P6ReportRenderer(
        service.database_path, service.state_root, clock=service.clock
    ).verify(args.run_id)
    view = service.inspect(args.run_id)
    manifest = view.report_manifest
    checks = {
        "local_ledger_valid": verification.get("valid") is True,
        "typed_records_match": False,
        "artifact_files_match": False,
        "reports_match": False,
    }
    if isinstance(manifest, P6ReportManifest):
        checks["typed_records_match"] = _typed_records_match(root, view)
        checks["artifact_files_match"] = _artifact_files_match(service, manifest, root)
        checks["reports_match"] = _reports_match(service, manifest, root)
    result = {
        "valid": all(checks.values()),
        "workflow": "p6",
        "run_id": str(view.run_id),
        "checks": checks,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["valid"] else 2


def _typed_records_match(root: Path, view) -> bool:
    expected = {
        "source_snapshot.json": view.source_snapshot.model_dump(mode="json"),
        "policy.json": view.policy.model_dump(mode="json"),
        "evidence.json": [item.model_dump(mode="json") for item in view.evidence],
        "assessment.json": None
        if view.assessment is None
        else view.assessment.model_dump(mode="json"),
        "claims.json": [item.model_dump(mode="json") for item in view.claims],
        "comparisons.json": [item.model_dump(mode="json") for item in view.comparisons],
    }
    return all(
        (root / "typed" / name).read_bytes() == canonical_json_bytes(value)
        for name, value in expected.items()
        if (root / "typed" / name).is_file()
    ) and all((root / "typed" / name).is_file() for name in expected)


def _artifact_files_match(service, manifest: P6ReportManifest, root: Path) -> bool:
    with SQLiteUnitOfWork(service.database_path, clock=service.clock) as uow:
        uow.begin()
        repository = ArtifactRecordRepository(uow.connection)
        store = ArtifactStore(service.state_root)
        try:
            for dependency in manifest.dependencies:
                record = repository.get(dependency.artifact_id)
                packet_path = root / "raw" / f"{dependency.artifact_id}.bin"
                if record is None or not packet_path.is_file():
                    return False
                content = packet_path.read_bytes()
                if (
                    hashlib.sha256(content).hexdigest() != dependency.content_hash
                    or len(content) != dependency.size_bytes
                    or content != store.read(record)
                ):
                    return False
        finally:
            uow.commit()
    return True


def _reports_match(service, manifest: P6ReportManifest, root: Path) -> bool:
    with SQLiteUnitOfWork(service.database_path, clock=service.clock) as uow:
        uow.begin()
        repository = ArtifactRecordRepository(uow.connection)
        store = ArtifactStore(service.state_root)
        markdown = repository.get(manifest.markdown_artifact_id)
        report_json = repository.get(manifest.json_artifact_id)
        if markdown is None or report_json is None:
            return False
        expected_md = store.read(markdown)
        expected_json = store.read(report_json)
        uow.commit()
    return (root / "report.md").read_bytes() == expected_md and (
        root / "report.json"
    ).read_bytes() == expected_json


if __name__ == "__main__":
    raise SystemExit(main())
