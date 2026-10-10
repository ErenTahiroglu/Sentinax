"""Phase 28B-1A: persistence adapter with injected reader and transport (no database here; the real PostgreSQL behaviour is in test_learning_persistence_postgres.py)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from backend.engine.learning.evidence_binding import EvidenceBindingError
from backend.engine.learning_store.evidence_binding_repository import BindingDisposition, EvidenceBindingRepository
from backend.tests.test_learning_evidence_binding import declaration, raw


class Fakes:
    def __init__(self, records, response=None):
        self.records = {r.id: r for r in records}
        self.calls = []
        self.response = response

    def reader(self, snapshot_id):
        return self.records.get(snapshot_id)

    def rpc(self, name, params):
        self.calls.append((name, params))
        if self.response is not None:
            return self.response(params)
        return [{"o_raw_snapshot_id": p["raw_snapshot_id"], "o_source_id": p["source_id"], "o_revision": p["revision"], "o_disposition": "INSERTED"} for p in params["p_bindings"]]


def repo(f: Fakes) -> EvidenceBindingRepository:
    return EvidenceBindingRepository(snapshot_reader=f.reader, rpc=f.rpc)


def test_valid_batch_goes_to_one_rpc_call_and_reports_dispositions() -> None:
    r1, r2 = raw(), raw(payload={"x": 2})
    f = Fakes([r1, r2])
    out = repo(f).persist((declaration(r1), declaration(r2)))
    assert [o.disposition for o in out] == [BindingDisposition.INSERTED] * 2
    assert len(f.calls) == 1 and f.calls[0][0] == "record_learning_evidence_bindings" and len(f.calls[0][1]["p_bindings"]) == 2


def test_nonexistent_snapshot_fails_closed_and_nothing_is_sent() -> None:
    r_ok, r_missing = raw(), raw(payload={"x": 2})
    f = Fakes([r_ok])
    with pytest.raises(EvidenceBindingError):
        repo(f).persist((declaration(r_ok), declaration(r_missing)))
    assert f.calls == []                                   # no partial batch


def test_failed_verification_in_any_item_sends_nothing() -> None:
    r1, r2 = raw(), raw(payload={"x": 2})
    d1, d2 = declaration(r1), declaration(r2)
    r2.raw_payload = {"x": 3}                                  # the retained payload no longer re-hashes to its stored hash
    f = Fakes([r1, r2])
    with pytest.raises(EvidenceBindingError):
        repo(f).persist((d1, d2))
    assert f.calls == []


def test_duplicate_logical_keys_in_one_batch_are_rejected_locally() -> None:
    r = raw()
    with pytest.raises(ValueError):
        repo(Fakes([r])).persist((declaration(r), declaration(r)))


def test_empty_or_non_tuple_input_rejected() -> None:
    f = Fakes([])
    for bad in ((), [], None):
        with pytest.raises((TypeError, ValueError)):
            repo(f).persist(bad)  # type: ignore[arg-type]
    assert f.calls == []


def test_malformed_or_mismatched_rpc_response_is_an_error_not_success() -> None:
    r = raw()
    for response in (lambda p: [], lambda p: [{"o_raw_snapshot_id": "x", "o_source_id": "y", "o_revision": 1, "o_disposition": "INSERTED"}],
                     lambda p: [{"o_raw_snapshot_id": p["p_bindings"][0]["raw_snapshot_id"], "o_source_id": p["p_bindings"][0]["source_id"], "o_revision": 1, "o_disposition": "WEIRD"}],
                     lambda p: None):
        f = Fakes([r], response=lambda params, resp=response: resp({"p_bindings": params["p_bindings"]}))
        with pytest.raises(RuntimeError):
            repo(f).persist((declaration(r),))


def test_constructor_requires_callables() -> None:
    with pytest.raises(TypeError):
        EvidenceBindingRepository(snapshot_reader=None, rpc=lambda n, p: [])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        EvidenceBindingRepository(snapshot_reader=lambda i: None, rpc="rpc")  # type: ignore[arg-type]


def test_reader_returning_a_foreign_object_is_rejected() -> None:
    r = raw()
    f = Fakes([r])
    f.reader = lambda snapshot_id: {"id": snapshot_id}      # type: ignore[assignment]
    with pytest.raises((EvidenceBindingError, TypeError)):
        repo(f).persist((declaration(r),))
    assert f.calls == []
