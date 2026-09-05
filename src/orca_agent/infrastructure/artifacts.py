"""Content-addressed, immutable artifact files for the P3 workflow."""

from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from orca_agent.application.errors import StateIntegrityError
from orca_agent.domain.ids import ActionId, ArtifactId, ExecutionId, RunId

from .clock import Clock, SystemClock
from .p3_records import ArtifactRecordRepository, StoredArtifact

_ARTIFACT_NAMESPACE = uuid.UUID("f0ef0e1b-3c6c-4fd7-9016-37d3406d2dbd")


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: ArtifactId
    content_hash: str
    size_bytes: int
    media_type: str
    relative_path: str


class ArtifactStore:
    """Store bytes below one configured root with traversal and symlink checks."""

    def __init__(self, state_root: str | Path, *, clock: Clock | None = None) -> None:
        self.state_root = Path(state_root).resolve()
        self.root = self.state_root / "artifacts"
        self.clock = clock or SystemClock()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(
        self,
        *,
        connection,
        run_id: RunId,
        content: bytes,
        media_type: str,
        action_id: ActionId | None = None,
        execution_id: ExecutionId | None = None,
    ) -> ArtifactRef:
        if not isinstance(content, bytes):
            raise TypeError("artifact content must be bytes")
        if not isinstance(media_type, str) or not media_type.strip() or "\x00" in media_type:
            raise ValueError("artifact media_type must be non-empty")
        digest = hashlib.sha256(content).hexdigest()
        relative = Path("sha256") / digest[:2] / digest
        destination = self._safe_path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._write_immutable(destination, content)
        artifact_id = ArtifactId(f"artifact_{uuid.uuid5(_ARTIFACT_NAMESPACE, digest).hex}")
        record = StoredArtifact(
            artifact_id=artifact_id,
            run_id=run_id,
            action_id=action_id,
            execution_id=execution_id,
            content_hash=digest,
            size_bytes=len(content),
            media_type=media_type.strip(),
            relative_path=relative.as_posix(),
            created_at_utc=self.clock.now_utc(),
        )
        existing = ArtifactRecordRepository(connection).get(artifact_id)
        if existing is None:
            ArtifactRecordRepository(connection).insert(record)
        elif (
            existing.run_id != record.run_id
            or existing.action_id != record.action_id
            or existing.execution_id != record.execution_id
            or existing.content_hash != record.content_hash
            or existing.relative_path != record.relative_path
            or existing.size_bytes != record.size_bytes
            or existing.media_type != record.media_type
        ):
            raise StateIntegrityError("artifact ID is bound to different content or owner")
        return ArtifactRef(
            artifact_id=artifact_id,
            content_hash=digest,
            size_bytes=len(content),
            media_type=record.media_type,
            relative_path=record.relative_path,
        )

    def read(self, record: StoredArtifact) -> bytes:
        path = self._safe_path(record.relative_path)
        if path.is_symlink() or not path.is_file():
            raise StateIntegrityError("artifact file is missing or is a symlink")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != record.content_hash:
            raise StateIntegrityError("artifact content hash does not match metadata")
        if len(content) != record.size_bytes:
            raise StateIntegrityError("artifact size does not match metadata")
        return content

    def path_for(self, relative_path: str) -> Path:
        return self._safe_path(relative_path)

    def _safe_path(self, relative: str | Path) -> Path:
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise StateIntegrityError("artifact path traversal is not allowed")
        cursor = self.root
        for part in candidate.parts:
            cursor /= part
            if cursor.is_symlink():
                raise StateIntegrityError("artifact path contains a symlink")
        resolved = (self.root / candidate).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as error:
            raise StateIntegrityError("artifact path escapes artifact root") from error
        return resolved

    @staticmethod
    def _write_immutable(destination: Path, content: bytes) -> None:
        if destination.exists():
            if destination.is_symlink() or not destination.is_file():
                raise StateIntegrityError("artifact destination is not a regular file")
            if destination.read_bytes() != content:
                raise StateIntegrityError("content-addressed artifact already differs")
            return
        fd, temporary_name = tempfile.mkstemp(prefix=".artifact-", dir=destination.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()


__all__ = ["ArtifactRef", "ArtifactStore"]
