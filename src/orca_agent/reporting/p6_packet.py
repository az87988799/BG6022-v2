"""Portable, bounded P6 ledger closure; restoration never executes packet SQL."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from orca_agent.application.errors import StateIntegrityError
from orca_agent.domain.canonical import canonical_json_bytes
from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import RunId
from orca_agent.domain.p6 import ComparabilityAssessment, P6ReportManifest
from orca_agent.infrastructure.artifacts import ArtifactStore
from orca_agent.infrastructure.p3_records import ArtifactRecordRepository, P5RecordRepository
from orca_agent.infrastructure.p6_records import P6RecordRepository
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork

TABLES = (
    "runs",
    "events",
    "interrupts",
    "outbox",
    "command_receipts",
    "workflow_records",
    "actions",
    "jobs",
    "artifacts",
    "evidence",
    "local_jobs",
)
REQUIRED = {
    "manifest.json",
    "report.md",
    "report.json",
    "typed/ledger.json",
    "typed/source_snapshot.json",
    "typed/policy.json",
    "typed/evidence.json",
    "typed/assessment.json",
    "typed/claims.json",
    "typed/comparisons.json",
}


def export_ledger(connection, state_root: Path, run_id: RunId, output: Path) -> None:
    """Include only selected P6/reference runs and their explicit P5 owners."""
    selected = {str(run_id)}
    pending = [run_id]
    records = P6RecordRepository(connection)
    while pending:
        current = pending.pop()
        for _, kind, item in records.list_p6_for_run(current):
            if kind == "p6.source_snapshot":
                selected.add(str(item.source_p5_run_id))
                selected.update(str(artifact.owner_run_id) for artifact in item.artifacts)
            elif isinstance(item, ComparabilityAssessment):
                reference = records.find_assessment(item.reference_assessment_id)
                if reference is None:
                    raise StateIntegrityError("packet reference assessment missing")
                if str(reference[0]) not in selected:
                    selected.add(str(reference[0]))
                    pending.append(reference[0])
    # Freeze the explicit P4 identity/method source, not a name-based lookup.
    for owner in tuple(selected):
        for _, kind, item in P5RecordRepository(connection).list_p5_for_run(RunId(owner)):
            if kind == "p5.execution_context":
                selected.add(str(item.source_run_id))
    tables = {}
    parameters = tuple(sorted(selected))
    placeholders = ",".join("?" for _ in parameters)
    for table in TABLES:
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
        rows = connection.execute(
            f"SELECT * FROM {table} WHERE run_id IN ({placeholders}) ORDER BY rowid", parameters
        ).fetchall()
        tables[table] = {"columns": columns, "rows": [list(row) for row in rows]}
    store = ArtifactStore(state_root)
    for owner in parameters:
        for artifact in ArtifactRecordRepository(connection).list_for_run(RunId(owner)):
            (output / "raw" / f"{artifact.artifact_id}.bin").write_bytes(store.read(artifact))
    (output / "typed" / "ledger.json").write_bytes(
        canonical_json_bytes(
            {"schema": "p6-ledger-closure/v1", "run_ids": list(parameters), "tables": tables}
        )
    )


def seal_packet(root: Path, manifest: P6ReportManifest) -> None:
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "packet_manifest.json":
            content = path.read_bytes()
            files[path.relative_to(root).as_posix()] = {
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
    packet = {
        "schema": "p6-archived-packet/v1",
        "verification_scope": "archived_packet",
        "run_id": str(manifest.run_id),
        "source_p5_run_id": str(manifest.source_p5_run_id),
        "manifest_hash": manifest.manifest_hash,
        "files": files,
    }
    packet["packet_hash"] = sha256_hex(packet)
    (root / "packet_manifest.json").write_bytes(canonical_json_bytes(packet))


def validate_packet(root: Path, run_id: RunId) -> P6ReportManifest:
    packet = json.loads((root / "packet_manifest.json").read_bytes())
    if set(packet) != {
        "schema",
        "verification_scope",
        "run_id",
        "source_p5_run_id",
        "manifest_hash",
        "files",
        "packet_hash",
    }:
        raise StateIntegrityError("packet manifest schema differs")
    expected_hash = packet.pop("packet_hash")
    if (
        sha256_hex(packet) != expected_hash
        or packet["schema"] != "p6-archived-packet/v1"
        or packet["verification_scope"] != "archived_packet"
        or packet["run_id"] != str(run_id)
        or not REQUIRED.issubset(packet["files"])
    ):
        raise StateIntegrityError("packet manifest binding is invalid")
    actual_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "packet_manifest.json"
    }
    if actual_paths != set(packet["files"]):
        raise StateIntegrityError("packet file closure is incomplete or contains extra files")
    for relative, expected in packet["files"].items():
        lexical = root / relative
        path = lexical.resolve()
        if (
            not path.is_relative_to(root.resolve())
            or lexical.is_symlink()
            or any(parent.is_symlink() for parent in lexical.parents)
        ):
            raise StateIntegrityError("packet path escapes its root")
        data = path.read_bytes()
        if (
            len(data) != expected["size_bytes"]
            or hashlib.sha256(data).hexdigest() != expected["sha256"]
        ):
            raise StateIntegrityError("packet file hash or size differs")
    manifest = P6ReportManifest.model_validate_json((root / "manifest.json").read_bytes())
    if (
        manifest.run_id != run_id
        or manifest.manifest_hash != packet["manifest_hash"]
        or str(manifest.source_p5_run_id) != packet["source_p5_run_id"]
    ):
        raise StateIntegrityError("packet report manifest is not bound")
    for dep in manifest.dependencies:
        data = (root / "raw" / f"{dep.artifact_id}.bin").read_bytes()
        if hashlib.sha256(data).hexdigest() != dep.content_hash or len(data) != dep.size_bytes:
            raise StateIntegrityError("report dependency differs from packet")
    return manifest


def restore_packet(root: Path, destination: Path) -> None:
    """Reconstruct a disposable ledger with fixed tables and parameterized inserts."""
    ledger = json.loads((root / "typed" / "ledger.json").read_bytes())
    if ledger["schema"] != "p6-ledger-closure/v1" or set(ledger["tables"]) != set(TABLES):
        raise StateIntegrityError("archived typed closure schema differs")
    with SQLiteUnitOfWork(destination / "state.sqlite3") as uow:
        uow.begin()
        uow.connection.execute("PRAGMA defer_foreign_keys=ON")
        for table in TABLES:
            expected = [row[1] for row in uow.connection.execute(f"PRAGMA table_info({table})")]
            value = ledger["tables"][table]
            if value["columns"] != expected:
                raise StateIntegrityError("archived table columns differ")
            placeholders = ",".join("?" for _ in expected)
            uow.connection.executemany(
                f"INSERT INTO {table} VALUES ({placeholders})", value["rows"]
            )
        for owner in ledger["run_ids"]:
            for artifact in ArtifactRecordRepository(uow.connection).list_for_run(RunId(owner)):
                target = (destination / "artifacts" / artifact.relative_path).resolve()
                if not target.is_relative_to((destination / "artifacts").resolve()):
                    raise StateIntegrityError("archived artifact path escapes destination")
                target.parent.mkdir(parents=True, exist_ok=True)
                data = (root / "raw" / f"{artifact.artifact_id}.bin").read_bytes()
                if (
                    hashlib.sha256(data).hexdigest() != artifact.content_hash
                    or len(data) != artifact.size_bytes
                ):
                    raise StateIntegrityError("archived artifact content differs")
                target.write_bytes(data)
        if uow.connection.execute("PRAGMA foreign_key_check").fetchall():
            raise StateIntegrityError("archived ledger foreign-key closure is incomplete")
        uow.commit()
