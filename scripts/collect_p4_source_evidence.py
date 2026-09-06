"""Export verified, lossless response evidence from an existing P4 smoke run."""

import argparse
import hashlib
import json
from pathlib import Path

from orca_agent.application.p4_service import P4ApplicationService
from orca_agent.domain.canonical import canonical_json_bytes
from orca_agent.domain.ids import RunId
from orca_agent.domain.p4 import ResponseEnvelope
from orca_agent.infrastructure.artifacts import ArtifactStore
from orca_agent.infrastructure.p3_records import ArtifactRecordRepository
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("evidence output must be a fresh file")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
    service = P4ApplicationService(manifest["state_root"])
    cases = []
    for case in manifest["cases"]:
        view = service.inspect(RunId(case["run_id"]))
        if view.state.phase.value != "awaiting_identity" or not view.attempts:
            raise ValueError("source smoke did not produce confirmable identity evidence")
        responses = []
        with SQLiteUnitOfWork(service.database_path) as uow:
            for attempt in view.attempts:
                record = ArtifactRecordRepository(uow.connection).get(attempt.response_artifact_id)
                content = ArtifactStore(service.state_root).read(record)
                envelope = ResponseEnvelope.model_validate_json(content)
                responses.append(
                    {
                        "artifact_sha256": hashlib.sha256(content).hexdigest(),
                        "artifact_relative_path": record.relative_path,
                        "envelope": envelope.model_dump(mode="json"),
                        "attempt": attempt.model_dump(mode="json"),
                    }
                )
        cases.append(
            {
                "name": case["name"],
                "run_id": str(view.run_id),
                "phase": view.state.phase.value,
                "query": view.query.model_dump(mode="json"),
                "candidate_bundle": view.candidate_bundle.model_dump(mode="json"),
                "responses": responses,
            }
        )
    result = {
        "provider": manifest["provider"],
        "cases": cases,
        "identity_confirmation": "PENDING_OWNER",
        "byte_format": "canonical UTF-8 JSON",
    }
    args.output.write_bytes(canonical_json_bytes(result))
    print(
        json.dumps(
            {
                "evidence": str(args.output),
                "cases": len(cases),
                "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
