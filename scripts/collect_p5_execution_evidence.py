"""Read-only P5 evidence collector.

The source database and artifacts are never repaired or rewritten.  The only
write is the explicitly requested evidence JSON output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from orca_agent.domain.ids import RunId
from orca_agent.infrastructure.artifacts import ArtifactRecordRepository
from orca_agent.infrastructure.p5_records import LocalJobRepository, P5RecordRepository
from orca_agent.infrastructure.repositories import EventRepository, RunRepository
from orca_agent.infrastructure.sqlite import resolve_database_path
from orca_agent.orchestration.p5_replay import verify_p5_snapshot
from orca_agent.orchestration.p5_versions import (
    P5_COMPILER_VERSION,
    P5_ENGINE_VERSION,
    P5_GEOMETRY_VERSION,
    P5_PARSER_VERSION,
    P5_POLICY_VERSION,
    P5_REGISTRY_VERSION,
    P5_SCHEMA_VERSION,
)


def _git_sha(root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip()


def _artifact_path(root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts or "\\" in relative_path:
        raise ValueError("artifact path escapes the artifact root")
    artifact_root = (root / "artifacts").resolve()
    target = (artifact_root / relative).resolve()
    target.relative_to(artifact_root)
    if target.is_symlink() or not target.is_file():
        raise ValueError("artifact file is missing or is a symlink")
    if os.name == "nt" and bool(
        getattr(target.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400
    ):
        raise ValueError("artifact file is a Windows reparse point")
    return target


def _verify_artifact(root: Path, record: Any) -> dict[str, object]:
    path = _artifact_path(root, record.relative_path)
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if digest != record.content_hash or len(content) != record.size_bytes:
        raise ValueError(f"artifact hash or size mismatch: {record.artifact_id}")
    return {
        "artifact_id": str(record.artifact_id),
        "run_id": str(record.run_id),
        "action_id": None if record.action_id is None else str(record.action_id),
        "execution_id": None if record.execution_id is None else str(record.execution_id),
        "role_path": record.relative_path,
        "media_type": record.media_type,
        "size_bytes": record.size_bytes,
        "sha256": digest,
    }


def _record_artifact_ids(records: tuple[tuple[object, str, object], ...]) -> set[str]:
    result: set[str] = set()
    for _record_id, _record_type, record in records:
        payload = record.model_dump(mode="json")
        for key, value in payload.items():
            if key.endswith("_artifact_id") and value is not None:
                result.add(str(value))
    return result


def collect(state_root: Path, run_id: RunId) -> dict[str, object]:
    database_path = resolve_database_path(state_root)
    if not database_path.is_file():
        raise ValueError("P5 state database does not exist")
    uri = f"file:{database_path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        snapshot = RunRepository(connection).require(run_id)
        events = EventRepository(connection).list_for_run(run_id)
        if (
            snapshot.schema_version != P5_SCHEMA_VERSION
            or snapshot.engine_version != P5_ENGINE_VERSION
        ):
            raise ValueError("run is not a schema-4 P5 run")
        p5_events = tuple(item.event for item in events)
        if not p5_events:
            raise ValueError("P5 event stream is empty")
        verify_p5_snapshot(
            snapshot=snapshot.state,
            stored_state_hash=snapshot.state_hash,
            stored_revision=snapshot.revision,
            stored_last_event_id=snapshot.last_event_id,
            events=p5_events,
        )
        records = P5RecordRepository(connection).list_p5_for_run(run_id)
        jobs = LocalJobRepository(connection).list_for_run(run_id)
        artifact_repository = ArtifactRecordRepository(connection)
        artifact_ids = _record_artifact_ids(records)
        artifacts = []
        for artifact_id in sorted(artifact_ids):
            record = artifact_repository.get(artifact_id)
            if record is None or record.run_id != run_id:
                raise ValueError(f"P5 artifact is missing or has the wrong owner: {artifact_id}")
            artifacts.append(_verify_artifact(state_root, record))
        p5_record_values = [
            {
                "record_id": str(record_id),
                "record_type": record_type,
                "record": record.model_dump(mode="json"),
            }
            for record_id, record_type, record in records
        ]
        job_values = [job.model_dump(mode="json") for _job_id, job in jobs]
        return {
            "valid": True,
            "implementation_sha": _git_sha(
                state_root.parent if (state_root / ".git").exists() else Path.cwd()
            ),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "rdkit": _rdkit_version(),
            "versions": {
                "schema": P5_SCHEMA_VERSION,
                "engine": P5_ENGINE_VERSION,
                "policy": P5_POLICY_VERSION,
                "registry": P5_REGISTRY_VERSION,
                "compiler": P5_COMPILER_VERSION,
                "parser": P5_PARSER_VERSION,
                "geometry": P5_GEOMETRY_VERSION,
            },
            "run_id": str(run_id),
            "phase": snapshot.state.phase.value,
            "status": snapshot.state.status.value,
            "source_run_id": str(snapshot.state.source_run_id),
            "records": p5_record_values,
            "jobs": job_values,
            "artifacts": artifacts,
            "scientific_assessment": "not_evaluated",
            "claim_status": "not_generated",
            "llm": "not_run",
        }
    finally:
        connection.close()


def _rdkit_version() -> str | None:
    try:
        from rdkit import rdBase
    except ModuleNotFoundError:
        return None
    return str(rdBase.rdkitVersion)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--run-id", type=RunId, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        evidence = collect(args.state_root.resolve(), args.run_id)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    except (OSError, sqlite3.Error, ValueError, TypeError) as error:
        print(json.dumps({"valid": False, "code": "evidence_invalid", "error": str(error)}))
        return 2
    print(json.dumps({"valid": True, "path": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
