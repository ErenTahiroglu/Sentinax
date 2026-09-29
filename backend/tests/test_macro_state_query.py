"""
backend/tests/test_macro_state_query.py
=======================================
Tests for the exact persisted PIT macro state query bridge (Phase 17B): migration 022's `value::text` RPC wrapper
and `MacroStateInputQueryService`, which hydrates the closed Phase 17A `MacroStateInputFact` without any float.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private.domain import AsOfMode, DataConfidenceLevel, DataStatus, SourceTier
from backend.engine.private.macro import state_query as module_under_test
from backend.engine.private.macro.registry import MacroSeriesRegistry
from backend.engine.private.macro.models import ContractStatus
from backend.engine.private.macro.state_inputs import MacroStateInputFact
from backend.engine.private.macro.state_query import MacroStateInputQueryService

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "supabase" / "migrations"
MIGRATION_022 = MIGRATIONS / "022_macro_state_input_exact_pit_rpc.sql"

UTC = timezone.utc
AS_OF = datetime(2026, 6, 30, 12, 0, 0, tzinfo=UTC)
BEFORE = AS_OF - timedelta(days=1)
AFTER = AS_OF + timedelta(days=1)
KEY = "TR_FX_USDTRY"
EFFECTIVE = date(2026, 6, 29)
OBS_ID = UUID(int=21)
SNAP_ID = UUID(int=22)

COLUMNS = ["canonical_key", "observation_id", "snapshot_id", "effective_date", "value_text", "data_status",
           "confidence_level", "source_tier", "published_at", "observed_at", "ingested_at", "superseded_at"]


# --- fake PostgREST client ---------------------------------------------------------------------------------------

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

    def table(self, name):  # any direct table access is a contract violation
        raise AssertionError(f"direct table access is forbidden: {name}")


def _row(**over) -> dict:
    row = dict(
        canonical_key=KEY, observation_id=str(OBS_ID), snapshot_id=str(SNAP_ID), effective_date="2026-06-29",
        value_text="47.500000", data_status="complete", confidence_level="high", source_tier="tier_1",
        published_at=BEFORE.isoformat(), observed_at=BEFORE.isoformat(), ingested_at=BEFORE.isoformat(),
        superseded_at=None)
    row.update(over)
    return row


def _query(data, **over):
    client = _Client(data)
    kw = dict(canonical_key=KEY, effective_date=EFFECTIVE, mode=AsOfMode.SYSTEM_AS_OF, as_of=AS_OF)
    kw.update(over)
    result = MacroStateInputQueryService(client).get_input(**kw)
    return result, client


def _fact(row_over=None, **over) -> MacroStateInputFact:
    result, _ = _query([_row(**(row_over or {}))], **over)
    assert result is not None
    return result


# --- service construction ------------------------------------------------------------------------------------------------

def test_constructor_requires_an_rpc_callable() -> None:
    class _NoRpc:
        pass

    class _NonCallable:
        rpc = "x"
    for bad in (None, _NoRpc(), _NonCallable(), object(), 5):
        with pytest.raises(TypeError, match=r"^client must expose a callable rpc$"):
            MacroStateInputQueryService(bad)
    assert MacroStateInputQueryService(_Client([]))


def test_get_input_is_keyword_only_and_has_no_implicit_current_query() -> None:
    params = inspect.signature(MacroStateInputQueryService.get_input).parameters
    assert [n for n in params if n != "self"] == ["canonical_key", "effective_date", "mode", "as_of"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for n, p in params.items() if n != "self")
    assert all(p.default is inspect.Parameter.empty for p in params.values())


# --- input validation -------------------------------------------------------------------------------------------------------

def test_input_types_are_exact_and_rpc_is_not_called() -> None:
    class _S(str):
        pass

    class _D(date):
        pass

    class _DT(datetime):
        pass
    cases = [
        dict(canonical_key=_S(KEY)), dict(canonical_key=None), dict(canonical_key=b"TR_FX_USDTRY"),
        dict(effective_date=datetime(2026, 6, 29, tzinfo=UTC)), dict(effective_date="2026-06-29"),
        dict(effective_date=_D(2026, 6, 29)), dict(mode="system_as_of"), dict(mode="SYSTEM_AS_OF"), dict(mode=None),
        dict(as_of=datetime(2026, 6, 30, 12, 0, 0)), dict(as_of="2026-06-30T12:00:00+00:00"), dict(as_of=None),
        dict(as_of=_DT(2026, 6, 30, 12, tzinfo=UTC)), dict(as_of=date(2026, 6, 30)),
    ]
    for over in cases:
        with pytest.raises(TypeError):
            _query([_row()], **over)
        _, client = _client_after(over)
        assert client.calls == []


def _client_after(over):
    client = _Client([_row()])
    kw = dict(canonical_key=KEY, effective_date=EFFECTIVE, mode=AsOfMode.SYSTEM_AS_OF, as_of=AS_OF)
    kw.update(over)
    try:
        MacroStateInputQueryService(client).get_input(**kw)
    except (TypeError, ValueError):
        pass
    return None, client


def test_empty_and_unnormalized_keys_are_rejected_before_rpc() -> None:
    for key in ("", "tr_fx_usdtry", " TR_FX_USDTRY", "TR_FX_USDTRY ", "TR_USDTRY"):
        with pytest.raises(ValueError):
            _query([_row()], canonical_key=key)
        assert _client_after(dict(canonical_key=key))[1].calls == []


def test_unverified_inactive_and_disabled_series_fail_before_rpc(monkeypatch) -> None:
    with pytest.raises(ValueError, match=r"^macro series must be active and VERIFIED$"):
        _query([_row()], canonical_key="TR_POLICY_RATE")
    assert _client_after(dict(canonical_key="TR_POLICY_RATE"))[1].calls == []
    base = MacroSeriesRegistry.get(KEY)
    for over in ({"is_active": False}, {"contract_status": ContractStatus.DISABLED},
                 {"contract_status": ContractStatus.UNVERIFIED}):
        monkeypatch.setitem(MacroSeriesRegistry._DEFINITIONS, KEY, dataclasses.replace(base, **over))
        with pytest.raises(ValueError, match=r"^macro series must be active and VERIFIED$"):
            _query([_row()])
        assert _client_after({})[1].calls == []
    monkeypatch.setitem(MacroSeriesRegistry._DEFINITIONS, KEY, base)
    with pytest.raises(ValueError, match=r"^macro series is not registered$"):
        _query([_row()], canonical_key="NOPE")


def test_policy_rate_is_never_aliased_to_aofm() -> None:
    _, client = _client_after(dict(canonical_key="TR_POLICY_RATE"))
    assert client.calls == []
    result, client = _query([_row(canonical_key="TR_TCMB_AOFM")], canonical_key="TR_TCMB_AOFM")
    assert result.canonical_key == "TR_TCMB_AOFM" and client.calls[0][1]["p_canonical_key"] == "TR_TCMB_AOFM"


# --- rpc invocation ------------------------------------------------------------------------------------------------------------

def test_rpc_is_called_once_with_exact_name_and_parameters() -> None:
    result, client = _query([_row()], mode=AsOfMode.SOURCE_AS_OF)
    assert client.calls == [(
        "get_pit_macro_state_input",
        {"p_canonical_key": KEY, "p_effective_date": "2026-06-29", "p_as_of": AS_OF.isoformat(),
         "p_as_of_mode": "source_as_of"},
    )]
    assert set(client.calls[0][1]) == {"p_canonical_key", "p_effective_date", "p_as_of", "p_as_of_mode"}
    assert result.mode is AsOfMode.SOURCE_AS_OF


def test_as_of_offset_is_not_normalized_for_the_fact() -> None:
    plus_three = timezone(timedelta(hours=3))
    as_of = datetime(2026, 6, 30, 15, 0, 0, tzinfo=plus_three)
    result, client = _query([_row()], as_of=as_of)
    assert client.calls[0][1]["p_as_of"] == "2026-06-30T15:00:00+03:00"
    assert result.as_of is as_of and result.as_of.utcoffset() == timedelta(hours=3)


def test_source_never_reads_the_macro_table_directly() -> None:
    tree = ast.parse(Path(module_under_test.__file__).read_text(encoding="utf-8"))
    assert not any(isinstance(n, ast.Attribute) and n.attr in {"table", "from_", "select"} for n in ast.walk(tree))
    assert not any(isinstance(n, ast.Constant) and n.value in {"macro_observations", "macro_series"}
                   for n in ast.walk(tree))
    assert any(isinstance(n, ast.Constant) and n.value == "get_pit_macro_state_input" for n in ast.walk(tree))


# --- cardinality / response shape ------------------------------------------------------------------------------------------------------

def test_empty_result_is_none_not_zero() -> None:
    result, client = _query([])
    assert result is None and len(client.calls) == 1


@pytest.mark.parametrize("bad", [None, {}, _row(), "x", b"x", 5, 0, (), ((_row()),), [None], ["x"], [1],
                                 [_row(), _row()], [_row(), _row(), _row()]])
def test_malformed_responses_fail_closed(bad) -> None:
    with pytest.raises((TypeError, ValueError)):
        _query(bad)


def test_missing_response_data_attribute_fails_closed() -> None:
    class _Bare:
        def execute(self):
            return object()

    class _C:
        def rpc(self, *a):
            return _Bare()
    with pytest.raises((TypeError, ValueError, AttributeError)):
        MacroStateInputQueryService(_C()).get_input(canonical_key=KEY, effective_date=EFFECTIVE,
                                                    mode=AsOfMode.SYSTEM_AS_OF, as_of=AS_OF)


def test_row_must_be_a_mapping_with_exactly_the_expected_keys() -> None:
    missing = _row()
    del missing["snapshot_id"]
    with pytest.raises(ValueError, match=r"^macro state RPC row must have exactly the expected columns$"):
        _query([missing])
    with pytest.raises(ValueError, match=r"^macro state RPC row must have exactly the expected columns$"):
        _query([{**_row(), "value": 1.5}])
    with pytest.raises(ValueError, match=r"^macro state RPC row must have exactly the expected columns$"):
        _query([{**_row(), "extra": None}])
    with pytest.raises(TypeError, match=r"^macro state RPC row must be a mapping$"):
        _query([list(_row().items())])


# --- identity ------------------------------------------------------------------------------------------------------------------------------

def test_identity_mismatches_are_rejected() -> None:
    with pytest.raises(ValueError, match=r"^macro state RPC row canonical_key does not match the request$"):
        _query([_row(canonical_key="TR_FX_EURTRY")])
    with pytest.raises(ValueError, match=r"^macro state RPC row effective_date does not match the request$"):
        _query([_row(effective_date="2026-06-28")])
    with pytest.raises(ValueError):
        _query([_row(effective_date="not-a-date")])
    with pytest.raises(TypeError):
        _query([_row(effective_date=date(2026, 6, 29))])
    with pytest.raises(TypeError):
        _query([_row(canonical_key=None)])


# --- exact decimal ----------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["0.000000", "47.500000", "-0.250000", "123456789012.123456",
                                  "999999999999.999999", "-999999999999.999999", "0.000001", "1E+2"])
def test_value_text_is_parsed_exactly_without_normalization(text: str) -> None:
    fact = _fact({"value_text": text})
    assert type(fact.value) is Decimal and str(fact.value) == str(Decimal(text)) and fact.value == Decimal(text)


def test_observed_zero_is_preserved_and_available() -> None:
    fact = _fact({"value_text": "0.000000"})
    assert fact.value == Decimal(0) and fact.value is not None and fact.is_available is True
    assert str(fact.value) == "0.000000"


def test_float_would_have_lost_this_value() -> None:
    text = "123456789012.123456"
    assert Decimal(str(float(text))) != Decimal(text) or float(text) != float(Decimal(text))  # transport hazard
    assert _fact({"value_text": text}).value == Decimal(text)


@pytest.mark.parametrize("bad", [47.5, 47, 0, 0.0, True, False, Decimal("47.5"), b"47.5", ["47.5"]])
def test_non_str_value_text_is_rejected(bad) -> None:
    with pytest.raises(TypeError, match=r"^value_text must be a str or None$"):
        _query([_row(value_text=bad)])


@pytest.mark.parametrize("bad", ["", " ", " 47.5", "47.5 ", "\t47.5", "47.5\n"])
def test_padded_or_empty_value_text_is_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match=r"^value_text must be a non-empty unpadded numeric string$"):
        _query([_row(value_text=bad)])


@pytest.mark.parametrize("bad", ["NaN", "sNaN", "Infinity", "-Infinity", "inf", "-inf"])
def test_non_finite_value_text_is_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        _query([_row(value_text=bad)])


@pytest.mark.parametrize("bad", ["abc", "1,5", "4 7", "0x10", "--1"])
def test_non_numeric_value_text_is_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match=r"^value_text must be a finite numeric string$"):
        _query([_row(value_text=bad)])


def test_null_value_text_with_unavailable_status_is_an_explicit_fact_not_none() -> None:
    fact = _fact({"value_text": None, "data_status": "unavailable", "confidence_level": "none"})
    assert isinstance(fact, MacroStateInputFact)
    assert fact.value is None and fact.is_available is False
    result, _ = _query([])
    assert result is None  # no eligible row is distinct from an explicit UNAVAILABLE row


# --- enums --------------------------------------------------------------------------------------------------------------------------------

def test_enum_hydration_is_exact() -> None:
    fact = _fact({"data_status": "partial", "confidence_level": "low", "source_tier": "tier_3"})
    assert (fact.data_status, fact.confidence_level, fact.source_tier) == (
        DataStatus.PARTIAL, DataConfidenceLevel.LOW, SourceTier.TIER_3_AGGREGATOR)


@pytest.mark.parametrize("field,bad", [
    ("data_status", "COMPLETE"), ("data_status", " complete"), ("data_status", "done"), ("data_status", ""),
    ("confidence_level", "HIGH"), ("confidence_level", "high "), ("confidence_level", "very_high"),
    ("source_tier", "TIER_1"), ("source_tier", "tier_9"), ("source_tier", "tier_1 "),
])
def test_unknown_or_unnormalized_enum_strings_fail_closed(field: str, bad: str) -> None:
    with pytest.raises(ValueError):
        _query([_row(**{field: bad})])


@pytest.mark.parametrize("field", ["data_status", "confidence_level", "source_tier"])
def test_non_str_or_null_enum_values_fail_closed(field: str) -> None:
    for bad in (None, 1, DataStatus.COMPLETE, True):
        with pytest.raises(TypeError):
            _query([_row(**{field: bad})])


# --- timestamps / uuids --------------------------------------------------------------------------------------------------------------------

def test_timestamps_are_parsed_as_aware_datetimes() -> None:
    fact = _fact({"published_at": "2026-06-29T10:00:00+00:00", "observed_at": "2026-06-29T09:00:00.123456+00:00",
                  "ingested_at": "2026-06-29T11:30:00+03:00", "superseded_at": "2026-07-05T00:00:00+00:00"})
    assert fact.published_at == datetime(2026, 6, 29, 10, tzinfo=UTC)
    assert fact.observed_at == datetime(2026, 6, 29, 9, 0, 0, 123456, tzinfo=UTC)
    assert fact.ingested_at.utcoffset() == timedelta(hours=3)
    assert fact.superseded_at == datetime(2026, 7, 5, tzinfo=UTC)
    assert _fact({"published_at": None}).published_at is None


@pytest.mark.parametrize("field", ["observed_at", "ingested_at"])
def test_required_timestamps_must_be_present(field: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        _query([_row(**{field: None})])


@pytest.mark.parametrize("field", ["published_at", "observed_at", "ingested_at", "superseded_at"])
def test_invalid_naive_or_non_str_timestamps_are_rejected(field: str) -> None:
    for bad in ("not-a-timestamp", "2026-13-40T00:00:00+00:00", "2026-06-29T10:00:00", "", 1751191200,
                BEFORE, date(2026, 6, 29)):
        with pytest.raises((TypeError, ValueError)):
            _query([_row(**{field: bad})])


def test_uuid_hydration() -> None:
    letters = UUID(int=0xABCDEF)  # contains hex letters, so upper-casing is a real change
    fact = _fact({"observation_id": str(letters), "snapshot_id": None})
    assert fact.observation_id == letters and fact.snapshot_id is None
    assert str(letters).upper() != str(letters)
    for bad in ("not-a-uuid", "", str(letters).upper(), str(letters).replace("-", ""), OBS_ID, 21, None):
        with pytest.raises((TypeError, ValueError)):
            _query([_row(observation_id=bad)])
    for bad in ("not-a-uuid", "", str(letters).upper(), SNAP_ID, 22):
        with pytest.raises((TypeError, ValueError)):
            _query([_row(snapshot_id=bad)])


# --- phase 17A defense propagation ------------------------------------------------------------------------------------------------------------------

def test_system_as_of_defenses_propagate() -> None:
    with pytest.raises(ValueError, match=r"^SYSTEM_AS_OF requires ingested_at <= as_of$"):
        _query([_row(ingested_at=AFTER.isoformat())])
    with pytest.raises(ValueError, match=r"^SYSTEM_AS_OF requires published_at <= as_of$"):
        _query([_row(published_at=AFTER.isoformat())])


def test_source_as_of_defenses_propagate() -> None:
    with pytest.raises(ValueError, match=r"^SOURCE_AS_OF requires the source-known timestamp <= as_of$"):
        _query([_row(published_at=AFTER.isoformat(), observed_at=BEFORE.isoformat())], mode=AsOfMode.SOURCE_AS_OF)
    with pytest.raises(ValueError, match=r"^SOURCE_AS_OF requires the source-known timestamp <= as_of$"):
        _query([_row(published_at=None, observed_at=AFTER.isoformat())], mode=AsOfMode.SOURCE_AS_OF)
    assert _fact({"published_at": BEFORE.isoformat(), "observed_at": AFTER.isoformat(),
                  "ingested_at": AFTER.isoformat()}, mode=AsOfMode.SOURCE_AS_OF).mode is AsOfMode.SOURCE_AS_OF


def test_supersession_cutoff_defense_propagates() -> None:
    for mode in (AsOfMode.SYSTEM_AS_OF, AsOfMode.SOURCE_AS_OF):
        for at in (BEFORE, AS_OF):
            with pytest.raises(ValueError, match=r"^a macro observation superseded at or before as_of is not eligible$"):
                _query([_row(superseded_at=at.isoformat())], mode=mode)
        assert _fact({"superseded_at": AFTER.isoformat()}, mode=mode).superseded_at == AFTER


def test_value_status_defenses_propagate() -> None:
    with pytest.raises(ValueError, match=r"^COMPLETE macro state input requires a finite Decimal value$"):
        _query([_row(value_text=None, data_status="complete")])
    with pytest.raises(ValueError, match=r"^UNAVAILABLE macro state input must not carry a value$"):
        _query([_row(value_text="0.000000", data_status="unavailable")])
    with pytest.raises(ValueError, match=r"^non-COMPLETE macro state input still requires an observed Decimal value$"):
        _query([_row(value_text=None, data_status="partial")])


def test_fact_uses_caller_mode_and_as_of_and_the_17a_builder() -> None:
    fact = _fact()
    assert type(fact) is MacroStateInputFact and fact.mode is AsOfMode.SYSTEM_AS_OF and fact.as_of is AS_OF
    assert (fact.canonical_key, fact.effective_date, fact.observation_id, fact.snapshot_id) == (
        KEY, EFFECTIVE, OBS_ID, SNAP_ID)
    source = Path(module_under_test.__file__).read_text(encoding="utf-8")
    assert "build_macro_state_input_fact" in source


# --- python purity / scope ----------------------------------------------------------------------------------------------------------------------------

def _imports() -> set[str]:
    tree = ast.parse(Path(module_under_test.__file__).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    return found


def test_imports_exclude_providers_orchestrator_funds_and_clients() -> None:
    imports = _imports()
    for banned in ("orchestrator", "providers", "fund_", "portfolio", "redis", "supabase", "os", "requests",
                   "httpx", "random", "uuid4", "json"):
        assert not any(banned == m or banned in m for m in imports if m != "uuid")
    tree = ast.parse(Path(module_under_test.__file__).read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "float" not in names and "uuid4" not in names and "now" not in names
    assert not any(isinstance(n, ast.Constant) and isinstance(n.value, float) for n in ast.walk(tree))
    assert not any(isinstance(n, ast.Attribute) and n.attr in {"now", "utcnow", "today", "getenv", "environ"}
                   for n in ast.walk(tree))


def test_module_is_clean_for_g1_g2_g4_g5_and_g3_only_flags_the_required_imports() -> None:
    from backend.tests.invariants import static_guards as sg

    rel = "backend/engine/private/macro/state_query.py"
    source = (sg.REPO_ROOT / rel).read_text(encoding="utf-8")
    assert sg.scan_g1(source, rel) == []
    assert sg.scan_g2(source, rel) == []
    assert sg.scan_g4(source, rel) == []
    assert sg.scan_g5(source, rel) == []
    assert {v.node_kind for v in sg.scan_g3(source, rel)} == {
        "PrivateImport:backend.engine.private.macro.state_inputs"}
    assert rel not in sg.PURE_MANIFEST
    assert "backend/engine/private/macro/state_inputs.py" not in sg.PURE_MANIFEST


# --- migration 022 (static contract) ---------------------------------------------------------------------------------------------------------------------

def _sql() -> str:
    return "\n".join(line.split("--", 1)[0] for line in MIGRATION_022.read_text(encoding="utf-8").splitlines())


def test_migration_022_exists_and_is_the_only_new_migration() -> None:
    assert MIGRATION_022.is_file()
    names = sorted(p.name for p in MIGRATIONS.glob("*.sql"))
    assert [n for n in names if n.startswith("022")] == [MIGRATION_022.name]
    assert max(names) == MIGRATION_022.name
    assert all(any(n.startswith(f"{i:03d}_") for n in names) for i in range(1, 22))


def test_migration_defines_exactly_one_function_with_the_exact_signature() -> None:
    sql = _sql()
    assert len(re.findall(r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION", sql, re.I)) == 1
    assert re.search(r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+public\.get_pit_macro_state_input\s*\(", sql, re.I)
    signature = re.search(r"FUNCTION\s+public\.get_pit_macro_state_input\s*\((.*?)\)\s*RETURNS", sql, re.I | re.S).group(1)
    params = [re.sub(r"\s+", " ", p.strip()) for p in signature.split(",")]
    assert params == ["p_canonical_key VARCHAR(64)", "p_effective_date DATE", "p_as_of TIMESTAMPTZ",
                      "p_as_of_mode VARCHAR(16)"]
    assert not re.search(r"\bDEFAULT\b", signature, re.I)


def test_migration_returns_exactly_the_listed_columns_in_order() -> None:
    sql = _sql()
    block = re.search(r"RETURNS\s+TABLE\s*\((.*?)\)\s*LANGUAGE", sql, re.I | re.S).group(1)
    names = [re.split(r"\s+", part.strip(), 1)[0] for part in re.split(r",\s*(?=\w+\s+[A-Za-z])", block)]
    assert names == COLUMNS
    assert re.search(r"value_text\s+TEXT", block, re.I)


def test_migration_transports_value_as_text_and_never_as_float() -> None:
    sql = _sql()
    assert re.search(r"o\.value::text\s+AS\s+value_text", sql, re.I)
    assert not re.search(r"o\.value\s+AS\s+value\b", sql, re.I)
    assert not re.search(r"\b(float|double\s+precision|real)\b|::\s*numeric|to_json|row_to_json", sql, re.I)
    assert not re.search(r"SELECT\s+\*|\bo\.\*|\bs\.\*", sql, re.I)


def test_migration_delegates_pit_selection_and_does_not_duplicate_it() -> None:
    sql = _sql()
    call = re.search(r"public\.get_pit_macro_observation\s*\((.*?)\)", sql, re.I | re.S)
    assert call is not None
    assert [a.strip() for a in call.group(1).split(",")] == ["p_canonical_key", "p_effective_date", "p_as_of",
                                                             "p_as_of_mode"]
    for duplicated in (r"ingested_at\s*<=", r"published_at\s*<=", r"superseded_at\s*>", r"COALESCE\s*\(",
                       r"FROM\s+public\.macro_observations", r"as_of_mode\s*=\s*'"):
        assert not re.search(duplicated, sql, re.I)


def test_migration_requires_active_verified_series() -> None:
    sql = _sql()
    assert re.search(r"JOIN\s+public\.macro_series\s+s\s+ON\s+s\.id\s*=\s*o\.macro_series_id", sql, re.I)
    assert re.search(r"s\.canonical_key\s*=\s*p_canonical_key", sql, re.I)
    assert re.search(r"s\.is_active\s+IS\s+TRUE", sql, re.I)
    assert re.search(r"s\.contract_status\s*=\s*'verified'", sql, re.I)


def test_migration_is_stable_security_invoker_with_explicit_search_path() -> None:
    sql = _sql()
    assert re.search(r"\bSTABLE\b", sql) and re.search(r"SECURITY\s+INVOKER", sql, re.I)
    assert not re.search(r"SECURITY\s+DEFINER", sql, re.I)
    assert re.search(r"SET\s+search_path\s*=\s*public\s*,\s*pg_temp", sql, re.I)
    assert not re.search(r"\b(VOLATILE|IMMUTABLE)\b", sql, re.I)


def test_migration_has_no_data_writes() -> None:
    sql = _sql()
    for keyword in (r"\bINSERT\b", r"\bUPDATE\b", r"\bDELETE\b", r"\bTRUNCATE\b", r"\bUPSERT\b",
                    r"ON\s+CONFLICT", r"\bALTER\b", r"\bDROP\b", r"\bCREATE\s+TABLE\b", r"\bCREATE\s+POLICY\b"):
        assert not re.search(keyword, sql, re.I), keyword


def test_migration_permissions() -> None:
    sql = _sql()
    fn = r"public\.get_pit_macro_state_input\s*\(\s*VARCHAR\(64\)\s*,\s*DATE\s*,\s*TIMESTAMPTZ\s*,\s*VARCHAR\(16\)\s*\)"
    assert re.search(rf"REVOKE\s+EXECUTE\s+ON\s+FUNCTION\s+{fn}\s+FROM\s+PUBLIC\s*;", sql, re.I)
    assert re.search(rf"REVOKE\s+EXECUTE\s+ON\s+FUNCTION\s+{fn}\s+FROM\s+anon\s*;", sql, re.I)
    grants = re.findall(rf"GRANT\s+EXECUTE\s+ON\s+FUNCTION\s+{fn}\s+TO\s+([^;]+);", sql, re.I)
    assert [re.sub(r"\s+", " ", g.strip()) for g in grants] == ["authenticated, service_role"]
    assert "anon" not in "".join(grants)
