"""
backend/engine/learning_store/evidence_binding_repository.py
============================================================
Verifies evidence-binding declarations against retained raw snapshots and persists them in ONE atomic RPC call (`record_learning_evidence_bindings`, migration 030).

Fail-closed ordering: every declaration is verified in Python (existence, source identity, retained payload, re-hash, retrieval instant) BEFORE anything is sent; one failure
sends nothing. The database independently re-checks reference existence, source identity, stored hash and retrieval instant, enforces append-only storage and idempotency.
The reader and transport are injected: this module performs no I/O of its own and never writes the raw-provider snapshot store.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional, Sequence, Tuple
from uuid import UUID

from backend.engine.learning._checks import require_exact_type
from backend.engine.learning.adapters import verify_evidence_binding
from backend.engine.learning.evidence_binding import EvidenceBindingDeclaration, EvidenceBindingError
from backend.engine.private.storage_models import RawProviderSnapshotRecord

RPC_NAME = "record_learning_evidence_bindings"


class BindingDisposition(Enum):
    INSERTED = "INSERTED"
    EXISTING = "EXISTING"


@dataclass(frozen=True)
class PersistedBinding:
    raw_snapshot_id: UUID
    source_id: str
    revision: int
    disposition: BindingDisposition


class EvidenceBindingRepository:
    def __init__(
        self,
        *,
        snapshot_reader: Callable[[UUID], Optional[RawProviderSnapshotRecord]],
        rpc: Callable[[str, dict], Sequence[Any]],
    ) -> None:
        if not callable(snapshot_reader) or not callable(rpc):
            raise TypeError("snapshot_reader and rpc must be callables")
        self._reader = snapshot_reader
        self._rpc = rpc

    def persist(self, declarations: Tuple[EvidenceBindingDeclaration, ...]) -> Tuple[PersistedBinding, ...]:
        require_exact_type("declarations", declarations, tuple)
        if not declarations:
            raise ValueError("declarations must not be empty")
        keys = []
        for d in declarations:
            require_exact_type("declaration", d, EvidenceBindingDeclaration)
            keys.append(d.logical_key())
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate (snapshot, source, revision) in one batch")

        payloads = []
        for d in declarations:
            record = self._reader(d.source_authority.raw_snapshot_id)
            payloads.append(verify_evidence_binding(d, record).to_rpc_dict())      # raises EvidenceBindingError; nothing has been sent yet

        rows = self._rpc(RPC_NAME, {"p_bindings": payloads})
        if not isinstance(rows, (list, tuple)) or len(rows) != len(payloads):
            raise RuntimeError("malformed persistence response: wrong row count")
        out = []
        for payload, row in zip(payloads, rows):
            try:
                same = (str(row["o_raw_snapshot_id"]) == payload["raw_snapshot_id"] and row["o_source_id"] == payload["source_id"]
                        and row["o_revision"] == payload["revision"])
                disposition = BindingDisposition(row["o_disposition"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(f"malformed persistence response row: {exc!r}") from exc
            if not same:
                raise RuntimeError("persistence response does not match the submitted binding")
            out.append(PersistedBinding(UUID(payload["raw_snapshot_id"]), payload["source_id"], payload["revision"], disposition))
        return tuple(out)
