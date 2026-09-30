"""
backend/tests/test_macro_state_history.py
=========================================
Tests for the PIT-safe exact macro history window (Phase 17C): migration 023's range RPC (candidate effective dates
-> migration-022 single-date authority) and `MacroStateInputQueryService.get_history`.
"""

from __future__ import annotations

import inspect
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private.domain import AsOfMode
from backend.engine.private.macro.registry import MacroSeriesRegistry
from backend.engine.private.macro.state_inputs import MacroStateInputFact
from backend.engine.private.macro.state_query import MacroStateInputQueryService

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "supabase" / "migrations"
MIGRATION_023 = MIGRATIONS / "023_macro_state_input_history_rpc.sql"

UTC = timezone.utc
AS_OF = datetime(2026, 6, 30, 12, 0, 0, tzinfo=UTC)
BEFORE = AS_OF - timedelta(days=1)
AFTER = AS_OF + timedelta(days=1)
KEY = "TR_FX_USDTRY"
START = date(2026, 1, 1)
END = date(2026, 6, 30)

COLUMNS = ["canonical_key", "observation_id", "snapshot_id", "effective_date", "value_text", "data_status",
           "confidence_level", "source_tier", "published_at", "observed_at", "ingested_at", "superseded_at"]


class _Response:
    def __init__(self, data) -> None:
        self.data = data


class _Query:
    def __init__(self, data) -> None:
        self._data = data

    def execute(self) -> _Response:
        return _Response(self._data)


class _Client:
    def __init__(self, data) -> None:
        self.data = data
        self.calls: list[tuple[str, dict]] = []

    def rpc(self, name, params):
        self.calls.append((name, params))
        return _Query(self.data)

    def table(self, name):
        raise AssertionError(f"direct table access is forbidden: {name}")


def _row(day: str = "2026-06-29", n: int = 1, **over) -> dict:
    row = dict(
        canonical_key=KEY, observation_id=str(UUID(int=100 + n)), snapshot_id=str(UUID(int=200 + n)),
        effective_date=day, value_text="47.500000", data_status="complete", confidence_level="high",
        source_tier="tier_1", published_at=BEFORE.isoformat(), observed_at=BEFORE.isoformat(),
        ingested_at=BEFORE.isoformat(), superseded_at=None)
    row.update(over)
    return row


def _kw(**over) -> dict:
    kw = dict(canonical_key=KEY, start_effective_date=START, end_effective_date=END, mode=AsOfMode.SYSTEM_AS_OF,
              as_of=AS_OF)
    kw.update(over)
    return kw


def _history(data, **over):
    client = _Client(data)
    return MacroStateInputQueryService(client).get_history(**_kw(**over)), client


def _calls_after(over) -> list:
    client = _Client([_row()])
    try:
        MacroStateInputQueryService(client).get_history(**_kw(**over))
    except (TypeError, ValueError):
        pass
    return client.calls


# --- python surface -------------------------------------------------------------------------------------------------------

def test_get_history_signature_is_keyword_only_without_defaults() -> None:
    params = inspect.signature(MacroStateInputQueryService.get_history).parameters
    assert [n for n in params if n != "self"] == [
        "canonical_key", "start_effective_date", "end_effective_date", "mode", "as_of"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for n, p in params.items() if n != "self")
    assert all(p.default is inspect.Parameter.empty for p in params.values())


def test_input_types_are_exact_and_rpc_is_not_called() -> None:
    class _S(str):
        pass

    class _D(date):
        pass

    class _DT(datetime):
        pass
    cases = [
        dict(canonical_key=_S(KEY)), dict(canonical_key=None), dict(canonical_key=b"TR_FX_USDTRY"),
        dict(start_effective_date=datetime(2026, 1, 1, tzinfo=UTC)), dict(start_effective_date="2026-01-01"),
        dict(start_effective_date=_D(2026, 1, 1)), dict(start_effective_date=None),
        dict(end_effective_date=datetime(2026, 6, 30, tzinfo=UTC)), dict(end_effective_date="2026-06-30"),
        dict(end_effective_date=_D(2026, 6, 30)), dict(end_effective_date=None),
        dict(mode="system_as_of"), dict(mode="SYSTEM_AS_OF"), dict(mode=None),
        dict(as_of=datetime(2026, 6, 30, 12, 0, 0)), dict(as_of="2026-06-30T12:00:00+00:00"), dict(as_of=None),
        dict(as_of=_DT(2026, 6, 30, 12, tzinfo=UTC)), dict(as_of=date(2026, 6, 30)),
    ]
    for over in cases:
        with pytest.raises(TypeError):
            _history([_row()], **over)
        assert _calls_after(over) == []


def test_bad_keys_are_rejected_before_rpc() -> None:
    for key in ("", "tr_fx_usdtry", " TR_FX_USDTRY", "TR_FX_USDTRY ", "NOPE"):
        with pytest.raises(ValueError):
            _history([_row()], canonical_key=key)
        assert _calls_after(dict(canonical_key=key)) == []


def test_unverified_series_fail_before_rpc() -> None:
    with pytest.raises(ValueError, match=r"^macro series must be active and VERIFIED$"):
        _history([_row()], canonical_key="TR_CPI_TUIK_YOY")
    assert _calls_after(dict(canonical_key="TR_CPI_TUIK_YOY")) == []
    assert MacroSeriesRegistry.get("TR_CPI_TUIK_YOY") is not None


def test_policy_rate_and_expected_inflation_histories_hydrate_exact_decimals() -> None:
    for key, texts in (("TR_POLICY_RATE", ("37.00", "37.00")), ("TR_EXPECTED_INFLATION_12M_PKA", ("23.69", "23.70"))):
        rows = [_row("2026-05-01", 1, canonical_key=key, value_text=texts[0]),
                _row("2026-06-01", 2, canonical_key=key, value_text=texts[1])]
        result, client = _history(rows, canonical_key=key)
        assert client.calls[0][1]["p_canonical_key"] == key
        assert [f.canonical_key for f in result] == [key, key]
        assert [str(f.value) for f in result] == list(texts) and all(type(f.value) is Decimal for f in result)
        assert [f.effective_date for f in result] == [date(2026, 5, 1), date(2026, 6, 1)]


def test_start_after_end_fails_before_rpc_and_is_not_swapped() -> None:
    over = dict(start_effective_date=END, end_effective_date=START)
    with pytest.raises(ValueError, match=r"^start_effective_date must not be after end_effective_date$"):
        _history([_row()], **over)
    assert _calls_after(over) == []


def test_single_day_range_is_valid() -> None:
    result, client = _history([_row("2026-06-29")], start_effective_date=date(2026, 6, 29),
                              end_effective_date=date(2026, 6, 29))
    assert len(result) == 1 and len(client.calls) == 1


# --- rpc invocation ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("mode,token", [(AsOfMode.SYSTEM_AS_OF, "SYSTEM_AS_OF"), (AsOfMode.SOURCE_AS_OF, "SOURCE_AS_OF")])
def test_rpc_called_once_with_exact_name_and_params(mode, token) -> None:
    _, client = _history([_row()], mode=mode)
    assert client.calls == [(
        "get_pit_macro_state_input_history",
        {"p_canonical_key": KEY, "p_start_effective_date": "2026-01-01", "p_end_effective_date": "2026-06-30",
         "p_as_of": AS_OF.isoformat(), "p_as_of_mode": token},
    )]


# --- response contract ------------------------------------------------------------------------------------------------

def test_empty_history_is_an_empty_tuple() -> None:
    result, client = _history([])
    assert type(result) is tuple and result == () and len(client.calls) == 1


@pytest.mark.parametrize("bad", [None, {}, {"a": 1}, 5, "x", b"x", (), ({},)])
def test_malformed_responses_fail_closed(bad) -> None:
    with pytest.raises((TypeError, ValueError)):
        _history(bad)


def test_missing_data_attribute_fails_closed() -> None:
    class _NoData:
        def rpc(self, name, params):
            class _Q:
                def execute(self):
                    return object()
            return _Q()
    with pytest.raises(ValueError):
        MacroStateInputQueryService(_NoData()).get_history(**_kw())


def test_multiple_rows_hydrate_to_ordered_immutable_tuple() -> None:
    rows = [_row("2026-01-01", 1), _row("2026-02-01", 2, value_text="1.000000"),
            _row("2026-04-01", 3, value_text="-3.250000")]
    result, _ = _history(rows)
    assert type(result) is tuple and len(result) == 3
    assert all(type(f) is MacroStateInputFact for f in result)
    assert [f.effective_date for f in result] == [date(2026, 1, 1), date(2026, 2, 1), date(2026, 4, 1)]
    assert [f.observation_id for f in result] == [UUID(int=101), UUID(int=102), UUID(int=103)]


def test_gaps_remain_gaps() -> None:
    result, _ = _history([_row("2026-01-01", 1), _row("2026-02-01", 2), _row("2026-04-01", 3)])
    assert date(2026, 3, 1) not in {f.effective_date for f in result}
    assert len(result) == 3


def test_row_must_be_mapping_with_exact_keys() -> None:
    with pytest.raises(TypeError, match=r"^macro state RPC row must be a mapping$"):
        _history([_row(), "x"])
    for key in COLUMNS:
        row = _row("2026-06-29")
        del row[key]
        with pytest.raises(ValueError, match=r"^macro state RPC row must have exactly the expected columns$"):
            _history([row])
    with pytest.raises(ValueError, match=r"^macro state RPC row must have exactly the expected columns$"):
        _history([_row(extra=1)])
    with pytest.raises(ValueError, match=r"^macro state RPC row must have exactly the expected columns$"):
        _history([_row("2026-01-01", 1), _row("2026-02-01", 2, extra=1)])


def test_identity_and_range_mismatches_reject_whole_history() -> None:
    with pytest.raises(ValueError, match=r"^macro state RPC row canonical_key does not match the request$"):
        _history([_row("2026-01-01", 1), _row("2026-02-01", 2, canonical_key="TR_TCMB_AOFM")])
    for day in ("2025-12-31", "2026-07-01"):
        with pytest.raises(ValueError, match=r"^macro state RPC row effective_date is outside the requested range$"):
            _history([_row("2026-01-01", 1), _row(day, 2)])
    # inclusive bounds
    result, _ = _history([_row("2026-01-01", 1), _row("2026-06-30", 2)])
    assert len(result) == 2


def test_order_and_uniqueness_are_verified_not_repaired() -> None:
    msg = r"^macro state RPC rows must have strictly increasing effective_date$"
    for rows in (
        [_row("2026-02-01", 1), _row("2026-02-01", 2)],
        [_row("2026-03-01", 1), _row("2026-02-01", 2)],
        [_row("2026-01-01", 1), _row("2026-03-01", 2), _row("2026-02-01", 3)],
        [_row("2026-01-01", 1), _row("2026-03-01", 2), _row("2026-03-01", 3)],
    ):
        with pytest.raises(ValueError, match=msg):
            _history(rows)


# --- exact decimal ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["0.000000", "-12.345678", "999999999999.123456", "47.500000", "0", "1E+2"])
def test_value_text_survives_exactly(text) -> None:
    result, _ = _history([_row("2026-01-01", 1), _row("2026-02-01", 2, value_text=text)])
    assert result[1].value == Decimal(text) and str(result[1].value) == str(Decimal(text))
    assert type(result[1].value) is Decimal


def test_zero_is_observed_and_none_only_with_unavailable() -> None:
    result, _ = _history([_row("2026-01-01", 1, value_text="0.000000"),
                          _row("2026-02-01", 2, value_text=None, data_status="unavailable")])
    assert result[0].value == Decimal("0.000000") and result[0].value is not None
    assert result[1].value is None and result[1].data_status.value == "unavailable"
    with pytest.raises(ValueError):
        _history([_row(value_text=None, data_status="complete")])


@pytest.mark.parametrize("bad", [47.5, 47, Decimal("47.5"), True])
def test_raw_numeric_transport_is_rejected(bad) -> None:
    with pytest.raises(TypeError, match=r"^value_text must be a str or None$"):
        _history([_row("2026-01-01", 1), _row("2026-02-01", 2, value_text=bad)])


@pytest.mark.parametrize("bad", ["", " 1", "1 ", "NaN", "Infinity", "abc"])
def test_bad_value_text_is_rejected(bad) -> None:
    with pytest.raises(ValueError):
        _history([_row(value_text=bad)])


# --- PIT propagation ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("mode", [AsOfMode.SYSTEM_AS_OF, AsOfMode.SOURCE_AS_OF])
def test_every_fact_shares_caller_mode_and_as_of(mode) -> None:
    result, _ = _history([_row("2026-01-01", 1), _row("2026-02-01", 2), _row("2026-03-01", 3)], mode=mode)
    assert all(f.mode is mode and f.as_of is AS_OF for f in result)


def test_one_pit_violation_fails_the_whole_history() -> None:
    good = _row("2026-01-01", 1)
    with pytest.raises(ValueError, match=r"^SYSTEM_AS_OF requires ingested_at <= as_of$"):
        _history([good, _row("2026-02-01", 2, ingested_at=AFTER.isoformat())])
    with pytest.raises(ValueError, match=r"^SOURCE_AS_OF requires the source-known timestamp <= as_of$"):
        _history([good, _row("2026-02-01", 2, published_at=AFTER.isoformat())], mode=AsOfMode.SOURCE_AS_OF)
    with pytest.raises(ValueError, match=r"^a macro observation superseded at or before as_of is not eligible$"):
        _history([good, _row("2026-02-01", 2, superseded_at=BEFORE.isoformat())])


def test_source_as_of_allows_late_ingest_and_history_is_not_sorted_in_python() -> None:
    late = dict(ingested_at=AFTER.isoformat(), observed_at=AFTER.isoformat(), published_at=BEFORE.isoformat())
    result, _ = _history([_row("2026-01-01", 1, **late)], mode=AsOfMode.SOURCE_AS_OF)
    assert len(result) == 1


# --- migration 023 (static contract) ---------------------------------------------------------------------------------------------

def _sql() -> str:
    return "\n".join(line.split("--", 1)[0] for line in MIGRATION_023.read_text(encoding="utf-8").splitlines())


def test_migration_023_exists() -> None:
    assert MIGRATION_023.is_file()
    names = sorted(p.name for p in MIGRATIONS.glob("*.sql"))
    assert [n for n in names if n.startswith("023")] == [MIGRATION_023.name]
    assert all(any(n.startswith(f"{i:03d}_") for n in names) for i in range(1, 23))


def test_exactly_one_function_with_exact_signature() -> None:
    sql = _sql()
    assert len(re.findall(r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION", sql, re.I)) == 1
    signature = re.search(
        r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+public\.get_pit_macro_state_input_history\s*\((.*?)\)\s*RETURNS",
        sql, re.I | re.S).group(1)
    params = [re.sub(r"\s+", " ", p.strip()) for p in signature.split(",")]
    assert params == ["p_canonical_key VARCHAR(64)", "p_start_effective_date DATE", "p_end_effective_date DATE",
                      "p_as_of TIMESTAMPTZ", "p_as_of_mode VARCHAR(16)"]
    assert not re.search(r"\bDEFAULT\b", signature, re.I)


def test_returns_exactly_the_twelve_columns_in_order() -> None:
    sql = _sql()
    block = re.search(r"RETURNS\s+TABLE\s*\((.*?)\)\s*LANGUAGE", sql, re.I | re.S).group(1)
    names = [re.split(r"\s+", part.strip(), 1)[0] for part in re.split(r",\s*(?=\w+\s+[A-Za-z])", block)]
    assert names == COLUMNS
    assert re.search(r"value_text\s+TEXT", block, re.I)


def test_language_volatility_security_and_search_path() -> None:
    sql = _sql()
    assert re.search(r"LANGUAGE\s+sql\b", sql, re.I)
    assert re.search(r"\bSTABLE\b", sql) and re.search(r"SECURITY\s+INVOKER", sql, re.I)
    assert not re.search(r"SECURITY\s+DEFINER|\b(VOLATILE|IMMUTABLE)\b", sql, re.I)
    assert re.search(r"SET\s+search_path\s*=\s*public\s*,\s*pg_temp", sql, re.I)


def test_permissions() -> None:
    sql = _sql()
    fn = (r"public\.get_pit_macro_state_input_history\s*\(\s*VARCHAR\(64\)\s*,\s*DATE\s*,\s*DATE\s*,\s*TIMESTAMPTZ"
          r"\s*,\s*VARCHAR\(16\)\s*\)")
    assert re.search(rf"REVOKE\s+EXECUTE\s+ON\s+FUNCTION\s+{fn}\s+FROM\s+PUBLIC\s*;", sql, re.I)
    assert re.search(rf"REVOKE\s+EXECUTE\s+ON\s+FUNCTION\s+{fn}\s+FROM\s+anon\s*;", sql, re.I)
    grants = re.findall(rf"GRANT\s+EXECUTE\s+ON\s+FUNCTION\s+{fn}\s+TO\s+([^;]+);", sql, re.I)
    assert [re.sub(r"\s+", " ", g.strip()) for g in grants] == ["authenticated, service_role"]


def test_no_writes_and_no_select_star() -> None:
    sql = _sql()
    for keyword in (r"\bINSERT\b", r"\bUPDATE\b", r"\bDELETE\b", r"\bTRUNCATE\b", r"\bUPSERT\b", r"ON\s+CONFLICT",
                    r"\bALTER\b", r"\bDROP\b", r"\bCREATE\s+TABLE\b", r"\bCREATE\s+POLICY\b"):
        assert not re.search(keyword, sql, re.I), keyword
    assert not re.search(r"SELECT\s+\*|\b\w+\.\*", sql, re.I)


def test_candidate_dates_are_distinct_inclusive_and_series_bound() -> None:
    sql = _sql()
    assert re.search(r"SELECT\s+DISTINCT\s+o\.effective_date", sql, re.I)
    assert re.search(r"FROM\s+public\.macro_observations\s+o", sql, re.I)
    assert re.search(r"JOIN\s+public\.macro_series\s+s\s+ON\s+s\.id\s*=\s*o\.macro_series_id", sql, re.I)
    assert re.search(r"s\.canonical_key\s*=\s*p_canonical_key", sql, re.I)
    assert re.search(r"o\.effective_date\s*>=\s*p_start_effective_date", sql, re.I)
    assert re.search(r"o\.effective_date\s*<=\s*p_end_effective_date", sql, re.I)
    assert re.search(r"s\.is_active\s+IS\s+TRUE", sql, re.I)
    assert re.search(r"s\.contract_status\s*=\s*'verified'", sql, re.I)


def test_candidate_enumeration_reads_no_analytical_columns() -> None:
    sql = _sql()
    candidates = re.search(r"FROM\s+\(\s*(SELECT\s+DISTINCT.*?)\)\s+AS\s+c\b", sql, re.I | re.S)
    assert candidates is not None
    body = candidates.group(1)
    for column in ("value", "data_status", "confidence", "published_at", "observed_at", "ingested_at",
                   "superseded_at", "source_tier", "snapshot_id"):
        assert not re.search(rf"\b{column}\w*\b", body, re.I), column


def test_candidate_dates_cannot_create_look_ahead_rows() -> None:
    """A candidate date ingested after as_of only reaches the 022 LATERAL call, which applies the real cutoff."""
    sql = _sql()
    assert re.search(r"CROSS\s+JOIN\s+LATERAL\s+public\.get_pit_macro_state_input\s*\(", sql, re.I)
    candidates = re.search(r"FROM\s+\(\s*(SELECT\s+DISTINCT.*?)\)\s+AS\s+c\b", sql, re.I | re.S).group(1)
    assert not re.search(r"p_as_of", candidates, re.I)


def test_calls_022_and_never_006_directly_forwarding_mode_unchanged() -> None:
    sql = _sql()
    call = re.search(r"public\.get_pit_macro_state_input\s*\((.*?)\)", sql, re.I | re.S)
    assert [a.strip() for a in call.group(1).split(",")] == ["p_canonical_key", "c.effective_date", "p_as_of",
                                                             "p_as_of_mode"]
    assert not re.search(r"get_pit_macro_observation|get_macro_observation_as_of", sql, re.I)
    assert not re.search(r"\b(upper|lower|casefold|initcap)\s*\(|as_of_mode\s*=\s*'", sql, re.I)


def test_no_duplicated_pit_predicates_or_numeric_recast() -> None:
    sql = _sql()
    for duplicated in (r"ingested_at\s*<=", r"published_at\s*<=", r"superseded_at\s*>", r"COALESCE\s*\(",
                       r"observed_at\s*<="):
        assert not re.search(duplicated, sql, re.I), duplicated
    assert not re.search(r"\b(float|double\s+precision|real)\b|::\s*numeric|to_json|row_to_json|::text|\bvalue\b",
                         sql, re.I)


def test_output_is_ordered_by_effective_date_ascending() -> None:
    sql = _sql()
    assert re.search(r"ORDER\s+BY\s+c\.effective_date\s+ASC\s*;?\s*\$\$", sql, re.I)


def test_earlier_migrations_are_untouched() -> None:
    import subprocess
    out = subprocess.run(["git", "diff", "--name-only", "HEAD", "--", "supabase/migrations"], cwd=REPO_ROOT,
                         capture_output=True, text=True, check=True).stdout.split()
    assert all(Path(p).name == MIGRATION_023.name for p in out)
