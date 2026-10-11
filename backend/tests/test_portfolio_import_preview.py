"""Phase 25D1E6: pure, read-only PREVIEW of a Sentinax Canonical CSV v1 transaction-history import.

The adapter (backend.engine.private.portfolio.import_preview) is a thin composition over the authoritative Phase 13 stages (staging/parser -> semantic interpreter ->
PIT instrument resolution). These tests cover only the NEW preview boundary; the hundreds of parser/semantic/resolution tests stay where they are.
All data below is SYNTHETIC. Nothing is persisted, no commit path is touched, and a preview never authorises a ledger commit.
"""
import ast
import dataclasses
import hashlib
import inspect
import json
import logging
import re
import socket
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from backend.engine.private.portfolio import import_preview as preview_module
from backend.engine.private.portfolio.import_preview import (
    PREVIEW_CONTRACT_VERSION,
    ImportPreview,
    ImportPreviewStatus,
    build_sentinax_canonical_csv_import_preview,
)
from backend.engine.private.portfolio.sentinax_csv_import import run_sentinax_canonical_csv_import_v1
from backend.tests.test_sentinax_canonical_csv_import import CANONICAL_HEADERS, MockInstrumentResolver, _make_csv

PORT = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")   # synthetic identities for provenance only
ACC = UUID("11111111-2222-4333-8444-555555555555")
AT = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)
D = date(2026, 8, 28)
BUY = "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,"
DEPOSIT = "cash_deposit,2026-08-28,2026-08-28T10:15:30+00:00,,,,,500.00,USD,,,,"
DIVIDEND = "dividend,2026-08-28,2026-08-28T10:15:30+00:00,MSFT,,,,50.00,USD,,,,"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
MARKER = "SENSITIVE-MARKER-4711"


AAPL_ID = UUID("00000000-0000-4000-8000-0000000000a1")   # fixed synthetic instrument ids keep digests reproducible
MSFT_ID = UUID("00000000-0000-4000-8000-0000000000a2")


def resolver(**extra):
    mapping = {("AAPL", D): [AAPL_ID], ("MSFT", D): [MSFT_ID]}
    mapping.update(extra)
    return MockInstrumentResolver(mapping=mapping)


def preview(content, res="default", **kw):
    if isinstance(content, (list, tuple)):
        content = _make_csv(content)
    return build_sentinax_canonical_csv_import_preview(
        portfolio_id=PORT, account_id=ACC, filename="synthetic.csv", content=content, imported_at=AT,
        resolver=resolver() if res == "default" else res, **kw)


def walk(obj):
    yield obj
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(k)
            yield from walk(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from walk(v)
    elif dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        for f in dataclasses.fields(obj):
            yield from walk(getattr(obj, f.name))


# --- 1-4: golden preview, determinism, provenance, ordering ---------------------------------------------------------------------------------------------------
def test_valid_canonical_file_is_structurally_previewable_but_never_commit_authorized():
    p = preview([BUY, DEPOSIT, DIVIDEND])
    assert isinstance(p, ImportPreview)
    assert p.contract_version == PREVIEW_CONTRACT_VERSION == "sentinax_import_preview.v1"
    assert p.status is ImportPreviewStatus.STRUCTURALLY_PREVIEWABLE
    assert (p.record_count, p.ready_count, p.rejected_count, p.unresolved_count) == (3, 3, 0, 0)
    assert (p.resolved_count, p.not_required_count, p.unresolved_instrument_count, p.ambiguous_instrument_count) == (2, 1, 0, 0)
    assert p.commit_authorized is False and p.persistent_state_changed is False and p.ledger_transaction_ids_generated is False
    assert "preview_only_no_commit_path" in p.blocking_reasons and "idempotency_and_deduplication_not_evaluated" in p.blocking_reasons
    assert p.file_error_code is None
    buy, deposit, dividend = p.records
    assert (buy.record_ordinal, buy.transaction_type, buy.instrument_reference, buy.quantity, buy.unit_price, buy.trade_currency) == (1, "buy", "AAPL", "10", "150.00", "USD")
    assert (buy.effective_date, buy.executed_at, buy.resolution_status, buy.assessment_status) == ("2026-08-28", "2026-08-28T10:15:30+00:00", "resolved", "ready")
    assert (deposit.transaction_type, deposit.cash_amount, deposit.cash_currency, deposit.resolution_status, deposit.instrument_reference) == ("cash_deposit", "500.00", "USD", "not_required", None)
    assert (dividend.transaction_type, dividend.cash_amount, dividend.instrument_reference) == ("dividend", "50.00", "MSFT")


def test_no_status_is_called_ready_or_committed_and_no_status_implies_authorisation():
    assert {s.value for s in ImportPreviewStatus} == {"file_invalid", "empty_valid", "has_rejected_records", "resolution_unavailable", "needs_resolution_review", "structurally_previewable"}
    for content, res in (([BUY], "default"), ([BUY], None), ([BUY, "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,x,150.00,USD,,,,,,"], "default"), (b"", "default")):
        assert preview(content, res).commit_authorized is False


def test_repeat_previews_are_identical_values_and_identical_json():
    a, b = preview([BUY, DEPOSIT, DIVIDEND]), preview([BUY, DEPOSIT, DIVIDEND])
    assert a == b
    assert json.dumps(a.to_dict(), sort_keys=True) == json.dumps(b.to_dict(), sort_keys=True)
    assert dataclasses.is_dataclass(a)
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.status = ImportPreviewStatus.FILE_INVALID  # type: ignore[misc]


def test_existing_phase13_provenance_and_digests_are_preserved_exactly():
    content = _make_csv([BUY, DEPOSIT, DIVIDEND])
    res = resolver()
    p = preview(content, res)
    authoritative = run_sentinax_canonical_csv_import_v1(portfolio_id=PORT, account_id=ACC, filename="synthetic.csv", content=content, imported_at=AT, resolver=res)
    assert p.file_content_sha256 == authoritative.file_content_sha256 == hashlib.sha256(content).hexdigest()
    assert (p.source_key, p.parser_revision) == (authoritative.source_key, authoritative.parser_revision)
    assert p.raw_manifest_sha256 == authoritative.raw_manifest_sha256
    assert p.parsed_manifest_sha256 == authoritative.parsed_manifest_sha256
    assert p.assessment_manifest_sha256 == authoritative.assessment_manifest_sha256
    assert p.draft_manifest_sha256 == authoritative.draft_manifest_sha256
    assert p.resolution_manifest_sha256 == authoritative.resolution_manifest_sha256
    assert (p.resolver_key, p.resolver_revision) == (res.resolver_key, res.resolver_revision)


def test_record_ordinals_follow_file_order_and_are_not_sorted():
    rows = [DIVIDEND, BUY, DEPOSIT]
    p = preview(rows)
    assert [r.record_ordinal for r in p.records] == [1, 2, 3]
    assert [r.transaction_type for r in p.records] == ["dividend", "buy", "cash_deposit"]
    q = preview(list(reversed(rows)))
    assert [r.transaction_type for r in q.records] == ["cash_deposit", "buy", "dividend"]
    assert p.file_content_sha256 != q.file_content_sha256


# --- 5: duplicates stay distinct --------------------------------------------------------------------------------------------------------------------------------
def test_identical_source_records_remain_distinct_and_are_never_deduplicated():
    p = preview([BUY, BUY, DEPOSIT, BUY])
    assert p.record_count == 4 and p.ready_count == 4
    assert [r.record_ordinal for r in p.records] == [1, 2, 3, 4]
    assert [r.identical_raw_record_of for r in p.records] == [None, 1, None, 1]            # informational only: bytes equal an earlier record
    assert p.status is ImportPreviewStatus.STRUCTURALLY_PREVIEWABLE
    assert "idempotency_and_deduplication_not_evaluated" in p.blocking_reasons


# --- 6-7: schema --------------------------------------------------------------------------------------------------------------------------------------------------
def test_missing_required_column_fails_closed_with_a_safe_code_and_no_records():
    header = CANONICAL_HEADERS.replace(",unit_price", "")
    p = preview((header + "\n" + "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,USD,,,,,,\n").encode())
    assert p.status is ImportPreviewStatus.FILE_INVALID and p.file_error_code and p.records == () and p.record_count == 0
    assert "file_invalid" in p.blocking_reasons and p.commit_authorized is False


def test_extra_column_fails_closed():
    p = preview(((CANONICAL_HEADERS + ",broker_note") + "\n" + BUY + ",x\n").encode())
    assert p.status is ImportPreviewStatus.FILE_INVALID and p.records == ()


# --- 8-11, 15: record-level semantic rejection (the authoritative interpreter decides; the preview only reports) --------------------------------------------------
@pytest.mark.parametrize("row, code", [
    ("buy,2026-13-40,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,", "invalid_effective_date"),
    ("buy,28/08/2026,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,", "invalid_effective_date"),
    ("buy,2026-08-28,2026-08-28T10:15:30,AAPL,10,150.00,USD,,,,,,", "invalid_executed_at"),
    ("buy,2026-08-28,2026-08-28 10:15:30+00:00,AAPL,10,150.00,USD,,,,,,", "invalid_executed_at"),
    ("buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,1e3,150.00,USD,,,,,,", "invalid_decimal"),
    ("buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,-5,150.00,USD,,,,,,", "invalid_decimal"),
    ("buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,.5,150.00,USD,,,,,,", "invalid_decimal"),
    ("buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10.,150.00,USD,,,,,,", "invalid_decimal"),
    ("buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,NaN,150.00,USD,,,,,,", "invalid_decimal"),
    ("buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,abc,USD,,,,,,", "invalid_decimal"),
    ("buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,usd,,,,,,", "invalid_currency"),
    ("buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,JPY,,,,,,", "invalid_currency"),
    ("buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,TL,,,,,,", "invalid_currency"),
    ("reversal,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,", "invalid_transaction_type"),
    ("BUY,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,", "invalid_transaction_type"),
    ("position,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,", "invalid_transaction_type"),
    ("buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,,150.00,USD,,,,,,", "invalid_economic_contract"),      # missing quantity is not zero
    ("buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,,USD,,,,,,", "invalid_economic_contract"),         # missing price is not inferred
    ("cash_deposit,2026-08-28,2026-08-28T10:15:30+00:00,,,,,500.00,,,,,", "invalid_economic_contract"),     # missing currency is not guessed
])
def test_rejected_rows_are_reported_with_static_diagnostics_and_no_fabricated_values(row, code):
    p = preview([DEPOSIT, row, DIVIDEND])
    assert p.status is ImportPreviewStatus.HAS_REJECTED_RECORDS
    assert (p.record_count, p.ready_count, p.rejected_count) == (3, 2, 1)
    bad = p.records[1]
    assert bad.record_ordinal == 2 and bad.assessment_status == "rejected"
    assert code in [d.code for d in bad.diagnostics]
    assert all(d.message and "\n" not in d.message for d in bad.diagnostics)
    for field in ("transaction_type", "effective_date", "executed_at", "instrument_reference", "quantity", "unit_price", "trade_currency", "cash_amount", "cash_currency",
                  "from_currency", "from_amount", "to_currency", "to_amount"):
        assert getattr(bad, field) is None, field                          # nothing from a rejected row is shown (no zero, no guess, no repair)
    assert bad.resolution_status == "not_evaluated"
    assert [r.record_ordinal for r in p.records if r.assessment_status == "ready"] == [1, 3]       # neighbours unaffected, ordinals preserved
    assert "rejected_records" in p.blocking_reasons and p.commit_authorized is False


def test_a_thousands_separator_comma_shifts_the_columns_and_fails_the_whole_file_closed():
    p = preview([DEPOSIT, "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,1,000,150.00,USD,,,,,,"])
    assert p.status is ImportPreviewStatus.FILE_INVALID and p.file_error_code == "csv_format_invalid" and p.records == ()


def test_rejected_value_text_is_never_echoed():
    p = preview([f"buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,{MARKER},150.00,USD,,,,,,"])
    assert MARKER not in json.dumps(p.to_dict()) and MARKER not in repr(p)


# --- 12-14: instrument resolution outcomes ------------------------------------------------------------------------------------------------------------------------
def test_ambiguous_instrument_needs_review_and_exposes_no_candidate_identifiers():
    res = MockInstrumentResolver(mapping={("DUAL", D): [uuid4(), uuid4()]})
    p = preview(["buy,2026-08-28,2026-08-28T10:15:30+00:00,DUAL,10,10.00,USD,,,,,,"], res)
    assert p.status is ImportPreviewStatus.NEEDS_RESOLUTION_REVIEW
    r = p.records[0]
    assert (r.resolution_status, r.candidate_count, r.resolution_diagnostic_codes) == ("ambiguous", 2, ("ambiguous_reference",))
    assert p.ambiguous_instrument_count == 1 and "ambiguous_instruments" in p.blocking_reasons
    assert not UUID_RE.search(json.dumps(p.to_dict()))


def test_unresolved_instrument_needs_review_and_is_never_marked_resolved():
    p = preview(["buy,2026-08-28,2026-08-28T10:15:30+00:00,UNKNOWN_TICKER,10,10.00,USD,,,,,,"], MockInstrumentResolver(mapping={}))
    assert p.status is ImportPreviewStatus.NEEDS_RESOLUTION_REVIEW
    r = p.records[0]
    assert (r.resolution_status, r.candidate_count, r.resolution_diagnostic_codes) == ("unresolved", 0, ("instrument_not_found",))
    assert p.unresolved_instrument_count == 1 and p.resolved_count == 0 and "unresolved_instruments" in p.blocking_reasons


def test_without_a_pit_resolver_instrument_records_are_not_evaluated_and_nothing_is_fabricated():
    p = preview([BUY, DEPOSIT], None)
    assert p.status is ImportPreviewStatus.RESOLUTION_UNAVAILABLE
    assert p.resolver_key is None and p.resolver_revision is None and p.resolution_manifest_sha256 is None
    assert [r.resolution_status for r in p.records] == ["not_evaluated", "not_evaluated"]
    assert p.resolved_count == 0 and "resolver_unavailable" in p.blocking_reasons and p.commit_authorized is False
    assert all(r.assessment_status == "ready" for r in p.records)               # assessment and economics are still reviewable


def test_a_cash_only_file_needs_no_resolver_and_is_fully_evaluated_without_one():
    p = preview([DEPOSIT, "fee,2026-08-28,2026-08-28T10:15:30+00:00,,,,,1.50,USD,,,,"], None)
    assert p.status is ImportPreviewStatus.STRUCTURALLY_PREVIEWABLE
    assert [r.resolution_status for r in p.records] == ["not_required", "not_required"]
    assert p.resolver_key is None


def test_a_failing_or_malformed_resolver_makes_resolution_unavailable_without_leaking_its_error_text():
    boom = MockInstrumentResolver(mapping={}, exception_on_ref="AAPL")
    p = preview([BUY], boom)
    assert p.status is ImportPreviewStatus.RESOLUTION_UNAVAILABLE and "resolver_error" in p.blocking_reasons
    assert "Resolver failed" not in json.dumps(p.to_dict()) and "AAPL" not in " ".join(p.blocking_reasons)
    bad = preview([BUY], MockInstrumentResolver(malformed_return=True))
    assert bad.status is ImportPreviewStatus.RESOLUTION_UNAVAILABLE
    assert preview([BUY], object()).status is ImportPreviewStatus.RESOLUTION_UNAVAILABLE


def test_instrument_text_is_preserved_verbatim_and_never_repaired_or_guessed():
    p = preview(["buy,2026-08-28,2026-08-28T10:15:30+00:00, AAPL ,10,150.00,USD,,,,,,"], resolver())      # the mapping knows "AAPL", not " AAPL "
    r = p.records[0]
    assert r.instrument_reference == " AAPL " and r.resolution_status == "unresolved"


# --- 21: empty valid file vs unavailable source ------------------------------------------------------------------------------------------------------------------
def test_a_valid_header_only_file_is_empty_valid_and_distinct_from_every_unavailable_or_invalid_state():
    empty = preview((CANONICAL_HEADERS + "\n").encode())
    assert empty.status is ImportPreviewStatus.EMPTY_VALID and empty.record_count == 0 and empty.records == ()
    assert empty.file_error_code is None and "no_records" in empty.blocking_reasons and empty.commit_authorized is False
    assert empty.file_content_sha256 == hashlib.sha256((CANONICAL_HEADERS + "\n").encode()).hexdigest()
    assert preview(b"").status is ImportPreviewStatus.FILE_INVALID
    assert preview([BUY], None).status is ImportPreviewStatus.RESOLUTION_UNAVAILABLE
    assert len({empty.status, preview(b"").status, preview([BUY], None).status}) == 3


@pytest.mark.parametrize("header", [
    "junk",
    "symbol,quantity,average_cost,currency",                                        # a current-holdings snapshot header
    CANONICAL_HEADERS + ",broker_note",                                             # extra column
    CANONICAL_HEADERS.replace(",unit_price", ""),                                   # missing column
    CANONICAL_HEADERS.replace("transaction_type", "type"),                          # renamed column
])
def test_a_header_only_file_with_a_non_canonical_header_is_file_invalid_never_empty_valid(header):
    p = preview((header + "\n").encode())
    assert p.status is ImportPreviewStatus.FILE_INVALID and p.file_error_code == "csv_schema_invalid"
    assert p.records == () and p.file_content_sha256 is None


def test_a_header_only_file_with_the_canonical_columns_in_another_order_is_still_empty_valid_like_phase13():
    reordered = ",".join(reversed(CANONICAL_HEADERS.split(",")))
    assert preview((reordered + "\n").encode()).status is ImportPreviewStatus.EMPTY_VALID


# --- input safety: strict canonical grammar, no silent repair ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("content", [
    b"", b"\xef\xbb\xbf" + CANONICAL_HEADERS.encode() + b"\n", CANONICAL_HEADERS.encode() + b"\x00\n",
    (CANONICAL_HEADERS + "\r\n" + BUY + "\n").encode(),                                    # mixed newline styles
    (CANONICAL_HEADERS + "\r" + BUY + "\r").encode(),                                      # bare CR
    (CANONICAL_HEADERS + "\n\n" + BUY + "\n").encode(),                                    # blank line
    (CANONICAL_HEADERS + "\n" + BUY + "\n").encode("utf-16"), b"\xff\xfe\x00bad",
    (CANONICAL_HEADERS + "\n" + 'buy,2026-08-28,2026-08-28T10:15:30+00:00,"AA\nPL",10,150.00,USD,,,,,,\n').encode(),   # multiline quoted field
    ("a" * 2_000_000).encode(), (",".join(f"c{i}" for i in range(200)) + "\n").encode(),
    (CANONICAL_HEADERS + ";" + "\n").encode(),
])
def test_format_violations_fail_closed_without_repair_or_leakage(content):
    p = preview(content)
    assert p.status is ImportPreviewStatus.FILE_INVALID and p.file_error_code and p.records == () and p.ready_count == 0
    assert re.fullmatch(r"[a-z][a-z0-9_]{0,63}", p.file_error_code)


def test_crlf_files_are_accepted_as_is_and_hashed_over_the_exact_original_bytes():
    content = (CANONICAL_HEADERS + "\r\n" + BUY + "\r\n" + DEPOSIT + "\r\n").encode()
    p = preview(content)
    assert p.status is ImportPreviewStatus.STRUCTURALLY_PREVIEWABLE and p.record_count == 2
    assert p.file_content_sha256 == hashlib.sha256(content).hexdigest()                    # no newline normalisation


def test_semantic_delimiters_are_not_guessed():
    p = preview(CANONICAL_HEADERS.replace(",", ";").encode() + b"\n" + BUY.replace(",", ";").encode() + b"\n")
    assert p.status is ImportPreviewStatus.FILE_INVALID


def test_the_input_bytes_object_is_not_mutated_or_retained():
    content = _make_csv([BUY])
    before = bytes(content)
    p = preview(content)
    assert content == before
    assert not any(isinstance(x, (bytes, bytearray, memoryview)) for x in walk(p))
    assert not any(isinstance(x, (bytes, bytearray, memoryview)) for x in walk(p.to_dict()))


# --- 22: holdings snapshots are never converted into trades -------------------------------------------------------------------------------------------------------
def test_a_current_holdings_snapshot_is_not_a_transaction_history_and_creates_no_trades():
    snapshot = b"symbol,quantity,average_cost,currency\nAAPL,10,150.00,USD\nMSFT,5,300.00,USD\n"
    p = preview(snapshot)
    assert p.status is ImportPreviewStatus.FILE_INVALID and p.record_count == 0 and p.records == ()
    assert "buy" not in json.dumps(p.to_dict()).lower().replace("file_invalid", "")
    assert p.ledger_transaction_ids_generated is False


def test_snapshot_semantics_inside_the_canonical_schema_are_rejected_not_reinterpreted_as_buys():
    rows = [f"{t},2026-08-28,,AAPL,10,150.00,USD,,,,,," for t in ("holding", "position", "snapshot", "opening_balance")]
    p = preview(rows)
    assert p.status is ImportPreviewStatus.HAS_REJECTED_RECORDS and p.ready_count == 0 and p.rejected_count == 4
    assert all(r.transaction_type is None and "invalid_transaction_type" in [d.code for d in r.diagnostics] for r in p.records)
    assert not any(r.transaction_type in ("buy", "sell") for r in p.records)


# --- 17-20: no ids, no I/O, no commit path, no retention, no sensitive logs ----------------------------------------------------------------------------------------
def test_no_ledger_transaction_id_or_any_uuid_is_generated_or_exposed():
    p = preview([BUY, DEPOSIT, DIVIDEND])
    assert not UUID_RE.search(json.dumps(p.to_dict())) and not UUID_RE.search(repr(p))
    src = inspect.getsource(preview_module)
    assert not re.search(r"uuid[145]\(|uuid\.uuid|\buuid4\b|\bgen_random_uuid\b", src)


def test_preview_works_while_every_commit_materialisation_repository_and_network_path_is_forbidden(monkeypatch):
    from backend.engine.private.portfolio import import_commit, import_materialization, repository, sentinax_csv_import_execution

    def boom(*a, **k):
        raise AssertionError("a forbidden path was reached")

    monkeypatch.setattr(import_commit, "build_import_ledger_binding_batch", boom)
    monkeypatch.setattr(import_materialization, "build_import_ledger_materialization_batch", boom)
    monkeypatch.setattr(repository.PortfolioRepository, "commit_import_binding_batch", boom, raising=False)
    monkeypatch.setattr(repository.PortfolioRepository, "__init__", boom)
    monkeypatch.setattr(sentinax_csv_import_execution, "run_sentinax_canonical_csv_import_execution_v1", boom, raising=False)
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket.socket, "connect_ex", boom)
    assert preview([BUY, DEPOSIT, DIVIDEND]).status is ImportPreviewStatus.STRUCTURALLY_PREVIEWABLE
    assert preview(b"junk").status is ImportPreviewStatus.FILE_INVALID


def test_the_module_imports_only_pure_phase13_stages_and_never_a_commit_repository_http_or_environment_module():
    tree = ast.parse(inspect.getsource(preview_module))
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            imported.add(n.module or "")
    forbidden = re.compile(r"repository|import_commit|import_batch_commit|materialization|import_commit_persistence|execution|postgrest|supabase|httpx|requests|aiohttp|socket|urllib|os$|subprocess|logging|sqlalchemy|psycopg")
    assert not [m for m in imported if forbidden.search(m)], imported
    src = inspect.getsource(preview_module)
    assert not re.search(r"commit_import_binding_batch|\.rpc\(|\.table\(|\.insert\(|\.execute\(|os\.environ|getenv|print\(|logger|open\(|datetime\.now|utcnow|time\.time", src)


def test_no_log_record_is_emitted_and_no_sensitive_value_reaches_logs_or_exceptions(caplog):
    caplog.set_level(logging.DEBUG)
    for content in (_make_csv([f"buy,2026-08-28,2026-08-28T10:15:30+00:00,{MARKER},{MARKER},150.00,USD,,,,,,"]), f"{MARKER},x\n{MARKER},y\n".encode(),
                    (CANONICAL_HEADERS + "\n" + f'"{MARKER}\n').encode()):
        p = preview(content, MockInstrumentResolver(mapping={}, exception_on_ref=MARKER))
        assert MARKER not in json.dumps(p.to_dict()) and MARKER not in repr(p)
        assert not p.file_error_code or MARKER.lower() not in p.file_error_code
    assert [r for r in caplog.records if MARKER in r.getMessage()] == []
    assert caplog.records == []


def test_the_returned_preview_never_contains_a_raw_record_line_or_the_file_text():
    content = _make_csv([BUY, DEPOSIT])
    blob = json.dumps(preview(content).to_dict()) + repr(preview(content))
    assert BUY not in blob and DEPOSIT not in blob and CANONICAL_HEADERS not in blob
    assert "filename" not in json.dumps(preview(content).to_dict()) and "synthetic.csv" not in blob


def test_the_serialised_contract_is_json_safe_decimal_text_only_with_a_closed_key_set():
    d = preview([BUY, DEPOSIT, "buy,2026-08-28,2026-08-28T10:15:30+00:00,UNKNOWN,x,1,USD,,,,,,"]).to_dict()
    json.dumps(d, allow_nan=False)
    assert not any(isinstance(x, float) for x in walk(d))
    assert set(d) == {"contract_version", "status", "source_key", "parser_revision", "file_content_sha256", "raw_manifest_sha256", "parsed_manifest_sha256", "assessment_manifest_sha256",
                      "draft_manifest_sha256", "resolution_manifest_sha256", "record_count", "ready_count", "rejected_count", "unresolved_count", "resolved_count", "not_required_count",
                      "unresolved_instrument_count", "ambiguous_instrument_count", "resolver_key", "resolver_revision", "blocking_reasons", "commit_authorized",
                      "persistent_state_changed", "ledger_transaction_ids_generated", "file_error_code", "records"}
    assert d["records"][0]["quantity"] == "10" and d["records"][0]["unit_price"] == "150.00"
    assert set(d["records"][0]) == {"record_ordinal", "assessment_status", "diagnostics", "transaction_type", "effective_date", "executed_at", "instrument_reference", "quantity", "unit_price",
                                    "trade_currency", "cash_amount", "cash_currency", "from_currency", "from_amount", "to_currency", "to_amount", "resolution_status",
                                    "resolution_diagnostic_codes", "candidate_count", "identical_raw_record_of"}


def test_counts_are_internally_consistent_for_mixed_files():
    p = preview([BUY, "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,x,1,USD,,,,,,", DEPOSIT, "buy,2026-08-28,2026-08-28T10:15:30+00:00,NOPE,1,1,USD,,,,,,"])
    assert p.ready_count + p.rejected_count + p.unresolved_count == p.record_count == 4
    assert p.resolved_count + p.not_required_count + p.unresolved_instrument_count + p.ambiguous_instrument_count == p.ready_count
    assert p.status is ImportPreviewStatus.HAS_REJECTED_RECORDS and {"rejected_records", "unresolved_instruments"} <= set(p.blocking_reasons)


# --- caller contract ---------------------------------------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("kw", [{"portfolio_id": "not-a-uuid"}, {"account_id": 5}, {"imported_at": datetime(2026, 8, 28)}, {"imported_at": "2026-08-28"}, {"content": "text"}, {"content": None}])
def test_invalid_caller_arguments_raise_instead_of_producing_a_preview(kw):
    args = dict(portfolio_id=PORT, account_id=ACC, filename="synthetic.csv", content=_make_csv([BUY]), imported_at=AT, resolver=None)
    args.update(kw)
    with pytest.raises((TypeError, ValueError)):
        build_sentinax_canonical_csv_import_preview(**args)


def test_arguments_are_keyword_only_and_have_no_defaults_for_identities_or_time():
    sig = inspect.signature(build_sentinax_canonical_csv_import_preview)
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in sig.parameters.values())
    for name in ("portfolio_id", "account_id", "filename", "content", "imported_at"):
        assert sig.parameters[name].default is inspect.Parameter.empty
    assert sig.parameters["resolver"].default is None


# --- documentation of the two import types -----------------------------------------------------------------------------------------------------------------------------
def test_the_two_import_types_and_the_no_invented_history_rule_are_documented():
    doc = (Path(__file__).resolve().parents[2] / "docs" / "PHASE_25D1E6_IMPORT_PREVIEW_FOUNDATION.md").read_text(encoding="utf-8")
    for needle in ("Transaction-history import", "Current-holdings snapshot", "never invent", "commit_import_binding_batch", "GET-only", "idempotency"):
        assert needle.lower() in doc.lower(), needle
