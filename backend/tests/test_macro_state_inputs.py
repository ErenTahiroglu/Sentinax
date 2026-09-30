"""
backend/tests/test_macro_state_inputs.py
========================================
Tests for the PIT-safe exact-Decimal macro state input authority (Phase 17A).

`MacroStateInputFact` is the only accepted analytical macro input: exact Decimal (never float-rehabilitated),
registry-bound (active + VERIFIED series only), explicit SYSTEM_AS_OF / SOURCE_AS_OF eligibility, missing != zero.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import domain
from backend.engine.private.domain import AsOfMode, DataConfidenceLevel, DataStatus, SourceTier
from backend.engine.private.macro import state_inputs as module_under_test
from backend.engine.private.macro.models import ContractStatus, MacroCategory, MacroFrequency, MacroUnit
from backend.engine.private.macro.registry import MacroSeriesRegistry
from backend.engine.private.macro.state_inputs import MacroStateInputFact, build_macro_state_input_fact

UTC = timezone.utc
AS_OF = datetime(2026, 6, 30, 12, 0, 0, tzinfo=UTC)
BEFORE = AS_OF - timedelta(days=1)
AFTER = AS_OF + timedelta(days=1)
_OBS_ID = UUID(int=11)
_SNAP_ID = UUID(int=12)
KEY = "TR_FX_USDTRY"


def _kw(**over) -> dict:
    kw = dict(
        canonical_key=KEY, effective_date=date(2026, 6, 29), value=Decimal("32.5"),
        data_status=DataStatus.COMPLETE, confidence_level=DataConfidenceLevel.HIGH,
        source_tier=SourceTier.TIER_1_REGULATORY, mode=AsOfMode.SYSTEM_AS_OF, as_of=AS_OF,
        published_at=BEFORE, observed_at=BEFORE, ingested_at=BEFORE, superseded_at=None,
        observation_id=_OBS_ID, snapshot_id=_SNAP_ID)
    kw.update(over)
    return kw


def _fact(**over) -> MacroStateInputFact:
    return build_macro_state_input_fact(**_kw(**over))


def _direct(**over) -> MacroStateInputFact:
    return MacroStateInputFact(**_kw(**over))


# --- shape ------------------------------------------------------------------------------------------------------

def test_fields_are_exact_and_frozen() -> None:
    assert [f.name for f in dataclasses.fields(MacroStateInputFact)] == [
        "canonical_key", "effective_date", "value", "data_status", "confidence_level", "source_tier", "mode",
        "as_of", "published_at", "observed_at", "ingested_at", "superseded_at", "observation_id", "snapshot_id"]
    fact = _fact()
    with pytest.raises(dataclasses.FrozenInstanceError):
        fact.value = Decimal(1)  # type: ignore[misc]


def test_builder_is_keyword_only_and_equals_constructor() -> None:
    params = inspect.signature(build_macro_state_input_fact).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())
    assert list(params) == [f.name for f in dataclasses.fields(MacroStateInputFact)]
    assert _fact() == _direct()
    with pytest.raises(TypeError):
        build_macro_state_input_fact(KEY)  # type: ignore[misc]


def test_no_scoring_or_technical_surface() -> None:
    fact = _fact()
    for name in ("score", "z_score", "growth_impulse", "policy_inflation_state", "financial_stress", "regime",
                 "tilt", "weight", "moving_average", "momentum", "rsi", "macd", "recommendation"):
        assert not hasattr(fact, name) and not hasattr(module_under_test, name)


# --- registry binding -----------------------------------------------------------------------------------------------

def test_registry_derived_properties_cannot_be_forged() -> None:
    fact = _fact()
    definition = MacroSeriesRegistry.get(KEY)
    assert (fact.category, fact.unit, fact.frequency, fact.geography, fact.provider) == (
        definition.category, definition.unit, definition.frequency, definition.geography, definition.provider)
    assert (fact.category, fact.unit, fact.frequency) == (MacroCategory.FX, MacroUnit.TRY,
                                                          MacroFrequency.BUSINESS_DAILY)
    field_names = {f.name for f in dataclasses.fields(MacroStateInputFact)}
    for name in ("unit", "frequency", "category", "provider", "geography"):
        assert name not in field_names
        with pytest.raises(TypeError):
            _fact(**{name: MacroUnit.PERCENT})  # caller cannot supply registry semantics


def test_unknown_and_unnormalized_series_are_rejected() -> None:
    for key in ("NOPE", "tr_fx_usdtry", " TR_FX_USDTRY", "TR_FX_USDTRY ", "TR_USDTRY"):
        with pytest.raises(ValueError, match=r"^macro series is not registered$"):
            _fact(canonical_key=key)


def test_policy_rate_is_verified_and_not_aliased_to_aofm() -> None:
    # At Phase 17A/17B creation TR_POLICY_RATE was unverified; Phase 17H verified and activated it.
    definition = MacroSeriesRegistry.get("TR_POLICY_RATE")
    assert definition.contract_status is ContractStatus.VERIFIED and definition.is_active is True
    policy = _fact(canonical_key="TR_POLICY_RATE", value=Decimal("37.00"))
    assert policy.canonical_key == "TR_POLICY_RATE" and policy.value == Decimal("37.00")
    assert type(policy.value) is Decimal and str(policy.value) == "37.00"
    assert (policy.category, policy.unit, policy.frequency, policy.geography, policy.provider) == (
        MacroCategory.INTEREST_RATE, MacroUnit.PERCENT, MacroFrequency.MONTHLY, "TR", "TCMB_EVDS")
    aofm = _fact(canonical_key="TR_TCMB_AOFM", value=Decimal("47.5"))
    assert aofm.canonical_key == "TR_TCMB_AOFM"  # AOFM is a distinct verified series, never a policy-rate alias
    assert aofm.frequency is MacroFrequency.BUSINESS_DAILY
    assert MacroSeriesRegistry.get("TR_TCMB_AOFM").verification_notes.endswith("policy rate.")


def test_expected_inflation_12m_is_accepted_with_registry_derived_taxonomy() -> None:
    fact = _fact(canonical_key="TR_EXPECTED_INFLATION_12M_PKA", value=Decimal("23.70"))
    assert fact.value == Decimal("23.70") and type(fact.value) is Decimal
    assert (fact.category, fact.unit, fact.frequency, fact.geography, fact.provider) == (
        MacroCategory.INFLATION_EXPECTATION, MacroUnit.PERCENT, MacroFrequency.MONTHLY, "TR", "TCMB_EVDS")


def test_still_unverified_series_remain_fail_closed() -> None:
    assert MacroSeriesRegistry.get("TR_CPI_TUIK_YOY").is_active is False
    with pytest.raises(ValueError, match=r"^macro series must be active and VERIFIED$"):
        _fact(canonical_key="TR_CPI_TUIK_YOY")


def test_inactive_or_non_verified_registry_entries_are_rejected(monkeypatch) -> None:
    base = MacroSeriesRegistry.get(KEY)
    for over in ({"is_active": False}, {"contract_status": ContractStatus.DISABLED},
                 {"contract_status": ContractStatus.UNVERIFIED}):
        monkeypatch.setitem(MacroSeriesRegistry._DEFINITIONS, KEY, dataclasses.replace(base, **over))
        with pytest.raises(ValueError, match=r"^macro series must be active and VERIFIED$"):
            _fact()
    monkeypatch.setitem(MacroSeriesRegistry._DEFINITIONS, KEY, base)
    assert _fact().canonical_key == KEY


# --- types ------------------------------------------------------------------------------------------------------------------

def test_canonical_key_type() -> None:
    class _S(str):
        pass
    for bad in (None, 1, b"TR_FX_USDTRY", _S(KEY)):
        with pytest.raises(TypeError):
            _fact(canonical_key=bad)
    with pytest.raises(ValueError):
        _fact(canonical_key="")


def test_effective_date_must_be_exact_date() -> None:
    class _D(date):
        pass
    for bad in (datetime(2026, 6, 29, tzinfo=UTC), "2026-06-29", None, _D(2026, 6, 29)):
        with pytest.raises(TypeError):
            _fact(effective_date=bad)


@pytest.mark.parametrize("field", ["as_of", "observed_at", "ingested_at"])
def test_required_datetimes_must_be_exact_aware(field: str) -> None:
    class _DT(datetime):
        pass
    for bad in (None, "2026-06-29T00:00:00+00:00", date(2026, 6, 29)):
        with pytest.raises(TypeError):
            _fact(**{field: bad})
    with pytest.raises(TypeError):
        _fact(**{field: datetime(2026, 6, 29)})  # naive
    with pytest.raises(TypeError):
        _fact(**{field: _DT(2026, 6, 29, tzinfo=UTC)})  # subclass


@pytest.mark.parametrize("field", ["published_at", "superseded_at"])
def test_optional_datetimes_must_be_none_or_exact_aware(field: str) -> None:
    class _DT(datetime):
        pass
    over = {field: None}
    if field == "superseded_at":
        assert _fact(**over).superseded_at is None
    for bad in ("2026-06-29", date(2026, 6, 29), datetime(2026, 6, 29), _DT(2026, 6, 29, tzinfo=UTC)):
        with pytest.raises(TypeError):
            _fact(**{field: bad})


def test_ids_enums_and_mode_types() -> None:
    class _U(UUID):
        pass
    for bad in (str(_OBS_ID), None, 11, _U(int=11)):
        with pytest.raises(TypeError):
            _fact(observation_id=bad)
    for bad in (str(_SNAP_ID), 12, _U(int=12)):
        with pytest.raises(TypeError):
            _fact(snapshot_id=bad)
    assert _fact(snapshot_id=None).snapshot_id is None
    for field, bad in (("data_status", "complete"), ("confidence_level", "high"), ("source_tier", "tier_1"),
                       ("mode", "system_as_of"), ("data_status", None), ("mode", None)):
        with pytest.raises(TypeError):
            _fact(**{field: bad})
    assert not hasattr(AsOfMode, "CURRENT_REPORTED")


# --- exact decimal boundary ---------------------------------------------------------------------------------------------------------

def test_value_accepts_only_exact_finite_decimal_or_none() -> None:
    class _D(Decimal):
        pass
    for bad in (32.5, 32, True, "32.5", Fraction(65, 2), _D("32.5")):
        with pytest.raises(TypeError):
            _fact(value=bad)
    for bad in (Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity")):
        with pytest.raises(ValueError):
            _fact(value=bad)


def test_decimal_value_is_preserved_exactly_without_normalization() -> None:
    for text in ("32.5", "0.1000000000000000055511151231257827", "1E+3", "-0.000001", "12345678901234567890.123456789"):
        value = Decimal(text)
        fact = _fact(value=value)
        assert fact.value is value and str(fact.value) == str(value)


def test_no_float_round_trip_in_module_source() -> None:
    tree = ast.parse(Path(module_under_test.__file__).read_text(encoding="utf-8"))
    assert not any(isinstance(n, ast.Name) and n.id in {"float", "str"} and isinstance(n.ctx, ast.Load)
                   and _is_call_func(tree, n) for n in ast.walk(tree) if isinstance(n, ast.Name))
    assert not any(isinstance(n, ast.Constant) and isinstance(n.value, float) for n in ast.walk(tree))


def _is_call_func(tree: ast.AST, name: ast.Name) -> bool:
    return any(isinstance(c, ast.Call) and c.func is name for c in ast.walk(tree))


# --- missing vs zero ---------------------------------------------------------------------------------------------------------------------

def test_zero_is_an_observation_and_none_is_missing() -> None:
    zero = _fact(value=Decimal("0"))
    assert zero.value == Decimal(0) and zero.value is not None and zero.is_available is True
    missing = _fact(value=None, data_status=DataStatus.UNAVAILABLE, confidence_level=DataConfidenceLevel.NONE)
    assert missing.value is None and missing.is_available is False


def test_complete_requires_a_decimal_and_unavailable_requires_none() -> None:
    with pytest.raises(ValueError, match=r"^COMPLETE macro state input requires a finite Decimal value$"):
        _fact(value=None, data_status=DataStatus.COMPLETE)
    with pytest.raises(ValueError, match=r"^UNAVAILABLE macro state input must not carry a value$"):
        _fact(value=Decimal("0"), data_status=DataStatus.UNAVAILABLE)
    with pytest.raises(ValueError, match=r"^UNAVAILABLE macro state input must not carry a value$"):
        _fact(value=Decimal("1.5"), data_status=DataStatus.UNAVAILABLE)


def test_partial_degraded_stale_keep_their_decimal_and_never_fabricate_zero() -> None:
    for status in (DataStatus.PARTIAL, DataStatus.DEGRADED, DataStatus.STALE):
        fact = _fact(data_status=status, value=Decimal("7.25"))
        assert fact.value == Decimal("7.25") and fact.is_available is True
        with pytest.raises(ValueError, match=r"^non-COMPLETE macro state input still requires an observed Decimal value$"):
            _fact(data_status=status, value=None)


def test_confidence_level_does_not_create_or_alter_availability() -> None:
    assert _fact(confidence_level=DataConfidenceLevel.NONE).is_available is True
    assert _fact(confidence_level=DataConfidenceLevel.LOW).value == Decimal("32.5")


# --- PIT: SYSTEM_AS_OF ----------------------------------------------------------------------------------------------------------------------------

def test_system_as_of_eligible_observation() -> None:
    fact = _fact(mode=AsOfMode.SYSTEM_AS_OF)
    assert fact.mode is AsOfMode.SYSTEM_AS_OF and fact.as_of == AS_OF
    assert _fact(published_at=None).published_at is None
    assert _fact(ingested_at=AS_OF).ingested_at == AS_OF          # boundary: ingested_at <= as_of
    assert _fact(published_at=AS_OF).published_at == AS_OF        # boundary: published_at <= as_of


def test_system_as_of_rejects_future_ingestion_and_publication() -> None:
    with pytest.raises(ValueError, match=r"^SYSTEM_AS_OF requires ingested_at <= as_of$"):
        _fact(ingested_at=AFTER)
    with pytest.raises(ValueError, match=r"^SYSTEM_AS_OF requires published_at <= as_of$"):
        _fact(published_at=AFTER)


def test_system_as_of_supersession() -> None:
    with pytest.raises(ValueError, match=r"^a macro observation superseded at or before as_of is not eligible$"):
        _fact(superseded_at=BEFORE)
    with pytest.raises(ValueError, match=r"^a macro observation superseded at or before as_of is not eligible$"):
        _fact(superseded_at=AS_OF)  # boundary: superseded_at must be strictly after as_of
    assert _fact(superseded_at=AFTER).superseded_at == AFTER


# --- PIT: SOURCE_AS_OF -----------------------------------------------------------------------------------------------------------------------------

def test_source_as_of_uses_published_at_else_observed_at() -> None:
    ok = _fact(mode=AsOfMode.SOURCE_AS_OF, published_at=BEFORE, observed_at=AFTER, ingested_at=AFTER)
    assert ok.mode is AsOfMode.SOURCE_AS_OF  # source-known = published_at; later observed/ingested do not matter
    fallback = _fact(mode=AsOfMode.SOURCE_AS_OF, published_at=None, observed_at=BEFORE, ingested_at=AFTER)
    assert fallback.published_at is None
    assert _fact(mode=AsOfMode.SOURCE_AS_OF, published_at=AS_OF).published_at == AS_OF  # boundary


def test_source_as_of_rejects_source_known_after_cutoff() -> None:
    with pytest.raises(ValueError, match=r"^SOURCE_AS_OF requires the source-known timestamp <= as_of$"):
        _fact(mode=AsOfMode.SOURCE_AS_OF, published_at=AFTER, observed_at=BEFORE)
    with pytest.raises(ValueError, match=r"^SOURCE_AS_OF requires the source-known timestamp <= as_of$"):
        _fact(mode=AsOfMode.SOURCE_AS_OF, published_at=None, observed_at=AFTER)


def test_source_as_of_supersession_follows_migration_006() -> None:
    with pytest.raises(ValueError, match=r"^a macro observation superseded at or before as_of is not eligible$"):
        _fact(mode=AsOfMode.SOURCE_AS_OF, superseded_at=BEFORE)
    with pytest.raises(ValueError, match=r"^a macro observation superseded at or before as_of is not eligible$"):
        _fact(mode=AsOfMode.SOURCE_AS_OF, superseded_at=AS_OF)
    assert _fact(mode=AsOfMode.SOURCE_AS_OF, superseded_at=AFTER).superseded_at == AFTER


def test_modes_are_not_interchangeable() -> None:
    # future ingestion is fatal for SYSTEM_AS_OF but irrelevant to SOURCE_AS_OF; future observation the reverse
    kw = dict(published_at=None, observed_at=AFTER, ingested_at=BEFORE)
    with pytest.raises(ValueError):
        _fact(mode=AsOfMode.SOURCE_AS_OF, **kw)
    assert _fact(mode=AsOfMode.SYSTEM_AS_OF, **kw).observed_at == AFTER


# --- availability property ------------------------------------------------------------------------------------------------------------------------

def test_is_available_semantics() -> None:
    assert _fact().is_available is True
    assert _fact(value=None, data_status=DataStatus.UNAVAILABLE).is_available is False


# --- purity / scope -----------------------------------------------------------------------------------------------------------------------------------

def _imports() -> set[str]:
    tree = ast.parse(Path(module_under_test.__file__).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    return found


def test_imports_are_minimal_and_exclude_providers_orchestrator_and_fund_modules() -> None:
    imports = _imports()
    private = {m for m in imports if m.startswith("backend.")}
    assert private <= {"backend.engine.private.domain", "backend.engine.private.macro.models",
                       "backend.engine.private.macro.registry"}
    assert "backend.engine.private.macro.registry" in private
    for banned in ("orchestrator", "providers", "fund_", "market_data", "portfolio", "redis", "supabase"):
        assert not any(banned in m for m in imports)
    assert imports <= {"__future__", "dataclasses", "datetime", "decimal", "uuid"} | private


def test_module_is_clean_for_g1_g2_g4_g5_and_g3_only_flags_the_required_imports() -> None:
    from backend.tests.invariants import static_guards as sg

    rel = "backend/engine/private/macro/state_inputs.py"
    source = (sg.REPO_ROOT / rel).read_text(encoding="utf-8")
    assert sg.scan_g1(source, rel) == []
    assert sg.scan_g2(source, rel) == []
    assert sg.scan_g4(source, rel) == []
    assert sg.scan_g5(source, rel) == []
    flagged = {v.node_kind for v in sg.scan_g3(source, rel)}
    assert flagged == {"PrivateImport:backend.engine.private.macro.models",
                       "PrivateImport:backend.engine.private.macro.registry"}
    assert rel not in sg.PURE_MANIFEST


def test_domain_enum_sources_are_the_project_enums() -> None:
    assert domain.AsOfMode is AsOfMode and domain.DataStatus is DataStatus
