"""Transactional ownership of project-wide physical execution capacity."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

RESOURCE_LOCAL_ORCA = "project.local_orca"


@dataclass(frozen=True)
class ExecutionResourceSlot:
    resource_key: str
    owner_execution_id: str | None
    owner_generation: int | None
    host_identity: str | None
    status: str
    acquired_at_utc: str | None
    released_at_utc: str | None
    evidence_ref: str | None


class ExecutionResourceRepository:
    """Small SQL façade; callers provide the transaction boundary."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def get(self, resource_key: str = RESOURCE_LOCAL_ORCA) -> ExecutionResourceSlot | None:
        row = self.connection.execute(
            "SELECT resource_key, owner_execution_id, owner_generation, host_identity, "
            "status, acquired_at_utc, released_at_utc, evidence_ref "
            "FROM execution_resource_slots WHERE resource_key=?",
            (resource_key,),
        ).fetchone()
        if row is None:
            return None
        return ExecutionResourceSlot(
            resource_key=str(row[0]),
            owner_execution_id=None if row[1] is None else str(row[1]),
            owner_generation=None if row[2] is None else int(row[2]),
            host_identity=None if row[3] is None else str(row[3]),
            status=str(row[4]),
            acquired_at_utc=None if row[5] is None else str(row[5]),
            released_at_utc=None if row[6] is None else str(row[6]),
            evidence_ref=None if row[7] is None else str(row[7]),
        )

    def try_acquire(
        self,
        *,
        execution_id: str,
        generation: int,
        host_identity: str,
        now_utc: str,
        resource_key: str = RESOURCE_LOCAL_ORCA,
    ) -> bool:
        cursor = self.connection.execute(
            "UPDATE execution_resource_slots SET owner_execution_id=?, "
            "owner_generation=?, host_identity=?, status='held', acquired_at_utc=?, "
            "released_at_utc=NULL, evidence_ref=NULL "
            "WHERE resource_key=? AND status='free'",
            (execution_id, generation, host_identity, now_utc, resource_key),
        )
        return cursor.rowcount == 1

    def mark_unknown(
        self,
        *,
        execution_id: str,
        generation: int,
        evidence_ref: str,
        resource_key: str = RESOURCE_LOCAL_ORCA,
    ) -> bool:
        cursor = self.connection.execute(
            "UPDATE execution_resource_slots SET status='unknown', evidence_ref=? "
            "WHERE resource_key=? AND owner_execution_id=? AND owner_generation=? "
            "AND status='held'",
            (evidence_ref, resource_key, execution_id, generation),
        )
        return cursor.rowcount == 1

    def release(
        self,
        *,
        execution_id: str,
        generation: int,
        now_utc: str,
        evidence_ref: str,
        resource_key: str = RESOURCE_LOCAL_ORCA,
    ) -> bool:
        cursor = self.connection.execute(
            "UPDATE execution_resource_slots SET owner_execution_id=NULL, "
            "owner_generation=NULL, host_identity=NULL, status='free', "
            "released_at_utc=?, evidence_ref=? "
            "WHERE resource_key=? AND owner_execution_id=? AND owner_generation=? "
            "AND status='held'",
            (now_utc, evidence_ref, resource_key, execution_id, generation),
        )
        return cursor.rowcount == 1


__all__ = ["ExecutionResourceRepository", "ExecutionResourceSlot", "RESOURCE_LOCAL_ORCA"]
