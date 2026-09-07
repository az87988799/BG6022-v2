"""Export a small, local-ledger P6 evidence packet without running ORCA."""

from __future__ import annotations

import argparse
from pathlib import Path

from orca_agent.application.p6_service import P6ApplicationService
from orca_agent.domain.canonical import canonical_json_bytes
from orca_agent.domain.ids import RunId
from orca_agent.domain.p6 import P6ReportManifest
from orca_agent.infrastructure.artifacts import ArtifactStore
from orca_agent.infrastructure.p3_records import ArtifactRecordRepository
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.reporting.p6_renderer import P6ReportRenderer, _find_manifest_artifact


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--run-id", type=RunId, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    service = P6ApplicationService(args.state_root)
    verification = P6ReportRenderer(
        service.database_path, service.state_root, clock=service.clock
    ).verify(args.run_id)
    if verification.get("valid") is not True:
        raise SystemExit("P6 report verification failed; refusing to export evidence")
    view = service.inspect(args.run_id)
    manifest = view.report_manifest
    if not isinstance(manifest, P6ReportManifest):
        raise SystemExit("P6 report manifest is missing")

    output = args.output.resolve()
    typed = output / "typed"
    raw = output / "raw"
    typed.mkdir(parents=True, exist_ok=True)
    raw.mkdir(parents=True, exist_ok=True)
    store = ArtifactStore(service.state_root)
    packet_artifacts: list[dict[str, object]] = []
    with SQLiteUnitOfWork(service.database_path, clock=service.clock) as uow:
        uow.begin()
        artifacts = ArtifactRecordRepository(uow.connection)
        for dependency in manifest.dependencies:
            record = artifacts.get(dependency.artifact_id)
            if record is None:
                raise SystemExit(f"manifest dependency is missing: {dependency.artifact_id}")
            content = store.read(record)
            relative = Path("raw") / f"{record.artifact_id}.bin"
            (output / relative).write_bytes(content)
            packet_artifacts.append(
                {
                    "artifact_id": str(record.artifact_id),
                    "content_hash": record.content_hash,
                    "size_bytes": record.size_bytes,
                    "media_type": record.media_type,
                    "owner_run_id": str(record.run_id),
                    "packet_path": relative.as_posix(),
                }
            )
        manifest_artifact = _find_manifest_artifact(
            connection=uow.connection,
            state_root=service.state_root,
            run_id=view.run_id,
            manifest=manifest,
        )
        (output / "manifest.json").write_bytes(store.read(manifest_artifact))
        for name, artifact_id in (
            ("report.md", manifest.markdown_artifact_id),
            ("report.json", manifest.json_artifact_id),
        ):
            artifact = artifacts.get(artifact_id)
            if artifact is None:
                raise SystemExit(f"report artifact is missing: {artifact_id}")
            (output / name).write_bytes(store.read(artifact))
        uow.commit()

    _write_json(typed / "source_snapshot.json", view.source_snapshot.model_dump(mode="json"))
    _write_json(typed / "policy.json", view.policy.model_dump(mode="json"))
    _write_json(typed / "evidence.json", [item.model_dump(mode="json") for item in view.evidence])
    _write_json(
        typed / "assessment.json",
        None if view.assessment is None else view.assessment.model_dump(mode="json"),
    )
    _write_json(typed / "claims.json", [item.model_dump(mode="json") for item in view.claims])
    _write_json(
        typed / "comparisons.json", [item.model_dump(mode="json") for item in view.comparisons]
    )
    _write_json(output / "verification.json", verification)
    _write_json(
        output / "packet_manifest.json",
        {
            "schema": "p6-local-ledger-export/v1",
            "verification_scope": manifest.verification_scope,
            "run_id": str(view.run_id),
            "source_p5_run_id": str(view.source_snapshot.source_p5_run_id),
            "source_snapshot_hash": view.source_snapshot.snapshot_hash,
            "report_manifest_id": str(manifest.report_manifest_id),
            "report_manifest_hash": manifest.manifest_hash,
            "artifacts": packet_artifacts,
            "typed_files": [
                "typed/source_snapshot.json",
                "typed/policy.json",
                "typed/evidence.json",
                "typed/assessment.json",
                "typed/claims.json",
                "typed/comparisons.json",
            ],
            "notes": [
                "This is an export from a verified local ledger, not a replacement "
                "for its event history.",
                "The source P5 artifacts retain their original owner IDs and hashes.",
            ],
        },
    )
    return 0


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(canonical_json_bytes(value))


if __name__ == "__main__":
    raise SystemExit(main())
