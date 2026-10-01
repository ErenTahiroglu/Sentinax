"""
backend/tests/test_game_changer_event.py
========================================
Phase 23A: source-neutral Game Changer event evidence with PIT knowledge binding and explicit revision lineage.
Evidence only: no sentiment, no materiality, no LLM, no quarantine, no portfolio consequence.
"""

from __future__ import annotations

import ast
import dataclasses
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import game_changer_event as module_under_test
from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.domain import AsOfMode, SourceTier
from backend.engine.private.game_changer_event import (
    GameChangerEvent,
    GameChangerEventPITBinding,
    GameChangerEventScope,
    GameChangerEventType,
    GameChangerRevisionKind,
    bind_game_changer_event_pit,
)
from backend.tests.invariants import static_guards as sg

UTC = timezone.utc
SHA = "a" * 64
UA, UB = UUID(int=1), UUID(int=2)
SC, TY, RK = GameChangerEventScope, GameChangerEventType, GameChangerRevisionKind


def T(y, m, d, h=0, mi=0, s=0, us=0, tz=UTC) -> datetime:
    return datetime(y, m, d, h, mi, s, us, tzinfo=tz)


def event(**changes) -> GameChangerEvent:
    values = dict(
        source_key="kap", source_event_key="EVT-1", source_tier=SourceTier.TIER_1_REGULATORY, scope=SC.INSTRUMENT,
        event_type=TY.FINANCIAL_REPORT, instrument_ids=(UA,), effective_date=None, published_at=T(2026, 9, 1, 10),
        observed_at=T(2026, 9, 1, 12), content_sha256=SHA, revision_kind=RK.ORIGINAL, revises_source_event_key=None,
    )
    values.update(changes)
    return GameChangerEvent(**values)


def ctx(mode, cutoff) -> AnalysisPITContext:
    return AnalysisPITContext(mode=mode, knowledge_cutoff=cutoff)


# --- enums / stored fields ---------------------------------------------------------------------------------------

def test_enums_and_stored_fields() -> None:
    assert [m.value for m in SC] == ["instrument", "systemic"]
    assert [m.value for m in TY] == ["financial_report", "guidance", "capital_allocation", "financing_liquidity", "m_and_a", "operations",
                                     "management_governance", "legal_regulatory", "ownership_control", "corporate_action", "macro_shock", "other"]
    assert [m.value for m in RK] == ["original", "update", "correction", "withdrawal"]
    assert [f.name for f in dataclasses.fields(GameChangerEvent)] == [
        "source_key", "source_event_key", "source_tier", "scope", "event_type", "instrument_ids", "effective_date", "published_at",
        "observed_at", "content_sha256", "revision_kind", "revises_source_event_key"]
    assert [f.name for f in dataclasses.fields(GameChangerEventPITBinding)] == ["event", "context"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        event().source_key = "x"  # type: ignore[misc]
    binding = bind_game_changer_event_pit(event=event(), context=ctx(AsOfMode.SOURCE_AS_OF, T(2026, 9, 2)))
    with pytest.raises(dataclasses.FrozenInstanceError):
        binding.event = event()  # type: ignore[misc]


def test_no_defaults() -> None:
    with pytest.raises(TypeError):
        GameChangerEvent(source_key="kap")  # type: ignore[call-arg]


# --- source identity ---------------------------------------------------------------------------------------------

def test_source_key_is_a_strict_canonical_identifier() -> None:
    for ok in ("kap", "sec_edgar" .replace("_", "-"), "manual.official", "a" * 64, "0x"):
        assert event(source_key=ok).source_key == ok
    for bad in ("", " kap", "kap ", "KAP", "-kap", "a" * 65, "https://kap.org.tr", "ka p", "kap\n", "_kap"):
        with pytest.raises(ValueError):
            event(source_key=bad)
    for bad in (None, 1, b"kap"):
        with pytest.raises(TypeError):
            event(source_key=bad)


def test_source_event_key_is_exact_trimmed_and_not_normalized() -> None:
    assert event(source_event_key="AbC-123/x y").source_event_key == "AbC-123/x y"      # case and inner spaces preserved
    assert event(source_event_key="x" * 128).source_event_key == "x" * 128
    for bad in ("", " ", "   ", " lead", "trail ", "x" * 129, "new\nline", "tab\there", "nul\x00", "del\x7f", " nbsp"):
        with pytest.raises(ValueError):
            event(source_event_key=bad)
    for bad in (None, 1, b"x"):
        with pytest.raises(TypeError):
            event(source_event_key=bad)


def test_source_tier_is_explicit_and_exact() -> None:
    for tier in SourceTier:
        assert event(source_tier=tier).source_tier is tier
    for bad in ("tier_1", None, 1, TY.OTHER):
        with pytest.raises(TypeError):
            event(source_tier=bad)
    assert event(source_key="kap", source_tier=SourceTier.TIER_5_PROXY).source_tier is SourceTier.TIER_5_PROXY   # never derived from the key


# --- scope / type / instruments ----------------------------------------------------------------------------------

def test_scope_instrument_compatibility() -> None:
    assert event().instrument_ids == (UA,)
    with pytest.raises(ValueError):
        event(instrument_ids=())                                                       # INSTRUMENT needs ids
    shock = event(scope=SC.SYSTEMIC, event_type=TY.MACRO_SHOCK, instrument_ids=())
    assert shock.instrument_ids == ()
    with pytest.raises(ValueError):
        event(scope=SC.SYSTEMIC, event_type=TY.MACRO_SHOCK, instrument_ids=(UA,))      # SYSTEMIC has no ids
    with pytest.raises(ValueError):
        event(scope=SC.INSTRUMENT, event_type=TY.MACRO_SHOCK)                          # MACRO_SHOCK is systemic only
    for kind in TY:
        if kind is not TY.MACRO_SHOCK:
            with pytest.raises(ValueError):
                event(scope=SC.SYSTEMIC, event_type=kind, instrument_ids=())           # unresolved company event is not systemic
            assert event(event_type=kind).event_type is kind


def test_scope_and_event_type_exactness() -> None:
    for bad in ("instrument", None, 1, TY.OTHER):
        with pytest.raises(TypeError):
            event(scope=bad)
    for bad in ("other", None, 1, SC.SYSTEMIC):
        with pytest.raises(TypeError):
            event(event_type=bad)


def test_instrument_ids_contract() -> None:
    class SubUUID(UUID):
        pass

    for bad in ([UA], None, (UA, "x"), "x", (SubUUID(int=1),)):
        with pytest.raises(TypeError):
            event(instrument_ids=bad)
    with pytest.raises(ValueError):
        event(instrument_ids=(UB, UA))                                                 # never silently sorted
    with pytest.raises(ValueError):
        event(instrument_ids=(UA, UA))
    assert event(instrument_ids=(UA, UB)).instrument_ids == (UA, UB)


# --- effective / published / observed ----------------------------------------------------------------------------

def test_effective_date_is_an_exact_date_or_none_and_may_be_in_the_future() -> None:
    class SubDate(date):
        pass

    assert event(effective_date=None).effective_date is None
    assert event(effective_date=date(2099, 1, 1)).effective_date == date(2099, 1, 1)
    for bad in (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 1), "2026-09-01", 20260901, SubDate(2026, 9, 1)):
        with pytest.raises(TypeError):
            event(effective_date=bad)


def test_published_and_observed_contracts() -> None:
    class SubDatetime(datetime):
        pass

    assert event(published_at=None).published_at is None                                # never fabricated
    for field in ("published_at", "observed_at"):
        for bad in (datetime(2026, 9, 1, 12), "2026-09-01T12:00:00Z", date(2026, 9, 1), 1,
                    SubDatetime(2026, 9, 1, 12, tzinfo=UTC)):
            with pytest.raises(TypeError):
                event(**{field: bad})
    with pytest.raises(TypeError):
        event(observed_at=None)
    with pytest.raises(ValueError):
        event(published_at=T(2026, 9, 1, 12, 0, 0, 1), observed_at=T(2026, 9, 1, 12))     # published_at <= observed_at
    assert event(published_at=T(2026, 9, 1, 12), observed_at=T(2026, 9, 1, 12)).published_at == T(2026, 9, 1, 12)
    plus9 = timezone(timedelta(hours=9))
    assert event(published_at=T(2026, 9, 1, 21, tz=plus9), observed_at=T(2026, 9, 1, 12)).published_at is not None     # 12:00Z == 12:00Z
    with pytest.raises(ValueError):
        event(published_at=T(2026, 9, 1, 21, 0, 0, 1, tz=plus9), observed_at=T(2026, 9, 1, 12))


def test_malformed_tzinfo_fails_closed() -> None:
    class Broken(tzinfo):
        def utcoffset(self, dt):
            raise RuntimeError("boom")

    class NoOffset(tzinfo):
        def utcoffset(self, dt):
            return None

    for tz in (Broken(), NoOffset()):
        for field in ("observed_at", "published_at"):
            with pytest.raises(TypeError):
                event(**{field: datetime(2026, 9, 1, 12, tzinfo=tz)})
    with pytest.raises(TypeError):
        event(observed_at=datetime(2026, 9, 1, 12))                                    # naive


# --- provenance hash ---------------------------------------------------------------------------------------------

def test_content_sha256_contract() -> None:
    assert event(content_sha256="0123456789abcdef" * 4).content_sha256 == "0123456789abcdef" * 4
    for bad in ("A" * 64, "a" * 63, "a" * 65, "g" * 64, "", "a" * 64 + "\n", "a" * 63 + "\n", " " + "a" * 63):
        with pytest.raises(ValueError):
            event(content_sha256=bad)
    for bad in (None, 1, b"a" * 64):
        with pytest.raises(TypeError):
            event(content_sha256=bad)


# --- revision lineage --------------------------------------------------------------------------------------------

def test_revision_lineage() -> None:
    assert event(revision_kind=RK.ORIGINAL, revises_source_event_key=None).revision_kind is RK.ORIGINAL
    with pytest.raises(ValueError):
        event(revision_kind=RK.ORIGINAL, revises_source_event_key="EVT-0")
    for kind in (RK.UPDATE, RK.CORRECTION, RK.WITHDRAWAL):
        assert event(revision_kind=kind, revises_source_event_key="EVT-0").revises_source_event_key == "EVT-0"
        with pytest.raises(ValueError):
            event(revision_kind=kind, revises_source_event_key=None)
        with pytest.raises(ValueError):
            event(revision_kind=kind, revises_source_event_key="EVT-1")                # revises itself
    for bad in ("", " x", "x ", "y" * 129, "a\nb"):
        with pytest.raises(ValueError):
            event(revision_kind=RK.UPDATE, revises_source_event_key=bad)
    for bad in (1, b"x"):
        with pytest.raises(TypeError):
            event(revision_kind=RK.UPDATE, revises_source_event_key=bad)
    for bad in ("update", None, 1):
        with pytest.raises(TypeError):
            event(revision_kind=bad)
    assert event(revision_kind=RK.UPDATE, revises_source_event_key="evt-1").revises_source_event_key == "evt-1"       # case-sensitive keys


# --- PIT binding -------------------------------------------------------------------------------------------------

def test_source_as_of_versus_system_as_of_divergence() -> None:
    e = event(published_at=T(2026, 9, 1, 10), observed_at=T(2026, 9, 1, 12))
    cutoff = T(2026, 9, 1, 11)
    bound = bind_game_changer_event_pit(event=e, context=ctx(AsOfMode.SOURCE_AS_OF, cutoff))
    assert bound.event is e
    with pytest.raises(ValueError):
        bind_game_changer_event_pit(event=e, context=ctx(AsOfMode.SYSTEM_AS_OF, cutoff))
    assert bind_game_changer_event_pit(event=e, context=ctx(AsOfMode.SYSTEM_AS_OF, T(2026, 9, 1, 12))).context.mode is AsOfMode.SYSTEM_AS_OF


def test_binding_retains_event_and_context_by_identity() -> None:
    e, c = event(), ctx(AsOfMode.SOURCE_AS_OF, T(2026, 9, 2))
    binding = bind_game_changer_event_pit(event=e, context=c)
    assert binding.event is e and binding.context is c
    assert GameChangerEventPITBinding(event=e, context=c) == binding


def test_missing_publication_falls_back_to_observed_at_without_tolerance() -> None:
    cutoff = T(2026, 9, 1, 12)
    at_cutoff = event(published_at=None, observed_at=cutoff)
    after = event(published_at=None, observed_at=cutoff + timedelta(microseconds=1))
    assert bind_game_changer_event_pit(event=at_cutoff, context=ctx(AsOfMode.SOURCE_AS_OF, cutoff)).event is at_cutoff
    with pytest.raises(ValueError):
        bind_game_changer_event_pit(event=after, context=ctx(AsOfMode.SOURCE_AS_OF, cutoff))
    assert bind_game_changer_event_pit(event=at_cutoff, context=ctx(AsOfMode.SYSTEM_AS_OF, cutoff)).event is at_cutoff
    with pytest.raises(ValueError):
        bind_game_changer_event_pit(event=after, context=ctx(AsOfMode.SYSTEM_AS_OF, cutoff))


def test_one_microsecond_boundaries_for_published_at() -> None:
    cutoff = T(2026, 9, 1, 11)
    exact = event(published_at=cutoff, observed_at=cutoff)
    late = event(published_at=cutoff + timedelta(microseconds=1), observed_at=cutoff + timedelta(microseconds=1))
    for mode in AsOfMode:
        assert bind_game_changer_event_pit(event=exact, context=ctx(mode, cutoff)).event is exact
        with pytest.raises(ValueError):
            bind_game_changer_event_pit(event=late, context=ctx(mode, cutoff))


def test_future_effective_date_is_not_lookahead() -> None:
    cutoff = T(2026, 9, 1, 12)
    announced = event(effective_date=date(2026, 12, 31), published_at=T(2026, 9, 1, 8), observed_at=T(2026, 9, 1, 9))
    assert announced.effective_date > cutoff.date()
    for mode in AsOfMode:
        assert bind_game_changer_event_pit(event=announced, context=ctx(mode, cutoff)).event is announced


def test_equal_instants_across_utc_offsets_are_equally_admissible() -> None:
    plus9, minus5 = timezone(timedelta(hours=9)), timezone(timedelta(hours=-5))
    published = T(2026, 9, 1, 19, tz=plus9)                                            # 10:00Z
    observed = T(2026, 9, 1, 7, tz=minus5)                                             # 12:00Z
    e = event(published_at=published, observed_at=observed)
    same_cutoff_a = ctx(AsOfMode.SOURCE_AS_OF, T(2026, 9, 1, 20, tz=plus9))            # 11:00Z
    same_cutoff_b = ctx(AsOfMode.SOURCE_AS_OF, T(2026, 9, 1, 6, tz=minus5))            # 11:00Z
    assert bind_game_changer_event_pit(event=e, context=same_cutoff_a).event is e
    assert bind_game_changer_event_pit(event=e, context=same_cutoff_b).event is e
    exact = ctx(AsOfMode.SYSTEM_AS_OF, T(2026, 9, 1, 7, tz=minus5))                    # equals observed_at
    assert bind_game_changer_event_pit(event=e, context=exact).event is e
    just_before = ctx(AsOfMode.SYSTEM_AS_OF, T(2026, 9, 1, 6, 59, 59, 999999, tz=minus5))
    with pytest.raises(ValueError):
        bind_game_changer_event_pit(event=e, context=just_before)


def test_binding_type_strictness() -> None:
    e, c = event(), ctx(AsOfMode.SOURCE_AS_OF, T(2026, 9, 2))

    class SubEvent(GameChangerEvent):
        pass

    class SubContext(AnalysisPITContext):
        pass

    sub_event = SubEvent(**{f.name: getattr(e, f.name) for f in dataclasses.fields(e)})
    sub_context = SubContext(mode=AsOfMode.SOURCE_AS_OF, knowledge_cutoff=T(2026, 9, 2))
    for bad_event, bad_context in ((object(), c), (None, c), (sub_event, c), (e, object()), (e, None), (e, sub_context)):
        with pytest.raises(TypeError):
            bind_game_changer_event_pit(event=bad_event, context=bad_context)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            GameChangerEventPITBinding(event=bad_event, context=bad_context)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        bind_game_changer_event_pit(e, c)  # type: ignore[misc]
    with pytest.raises(TypeError):
        bind_game_changer_event_pit(event=e)  # type: ignore[call-arg]


def test_direct_binding_construction_enforces_temporal_admissibility() -> None:
    late = event(published_at=None, observed_at=T(2026, 9, 3))
    with pytest.raises(ValueError):
        GameChangerEventPITBinding(event=late, context=ctx(AsOfMode.SOURCE_AS_OF, T(2026, 9, 2)))


def test_revision_and_withdrawal_events_are_bound_without_chain_resolution() -> None:
    correction = event(source_event_key="EVT-2", revision_kind=RK.CORRECTION, revises_source_event_key="EVT-NEVER-SEEN")
    binding = bind_game_changer_event_pit(event=correction, context=ctx(AsOfMode.SOURCE_AS_OF, T(2026, 9, 2)))
    assert binding.event.revises_source_event_key == "EVT-NEVER-SEEN"                   # lineage is recorded, never chased


def test_systemic_event_binds() -> None:
    shock = event(scope=SC.SYSTEMIC, event_type=TY.MACRO_SHOCK, instrument_ids=(), source_key="manual-official", source_tier=SourceTier.TIER_4_DERIVED)
    assert bind_game_changer_event_pit(event=shock, context=ctx(AsOfMode.SYSTEM_AS_OF, T(2026, 9, 2))).event is shock


# --- scope guards ------------------------------------------------------------------------------------------------

_SOURCE = Path(module_under_test.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/game_changer_event.py"


def test_module_is_registered_pure_and_clean_on_g1_to_g5() -> None:
    assert _REL in sg.PURE_MANIFEST
    for scanner in (sg.scan_g1, sg.scan_g2, sg.scan_g3, sg.scan_g4, sg.scan_g5):
        assert scanner(_SOURCE, _REL) == []


def test_imports_are_standard_library_pit_context_and_domain_enums_only() -> None:
    names: set[str] = set()
    imported: dict[str, set[str]] = {}
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            names.add(node.module or "")
            imported.setdefault(node.module or "", set()).update(alias.name for alias in node.names)
    assert names <= {"__future__", "dataclasses", "datetime", "enum", "re", "uuid", "backend.engine.private.analysis_pit",
                     "backend.engine.private.domain"}
    assert imported["backend.engine.private.analysis_pit"] == {"AnalysisPITContext"}
    assert imported["backend.engine.private.domain"] == {"AsOfMode", "SourceTier"}
    for forbidden in ("provider", "kap", "scraper", "requests", "httpx", "browser", "selenium", "playwright", "llm", "openai", "gemini",
                      "claude", "langchain", "langgraph", "sec", "market_data", "portfolio", "allocation", "rebalance", "fee_tax",
                      "buffett", "database", "repository", "supabase", "random", "numpy", "scipy", "pandas", "decimal"):
        assert not any(forbidden in name.split(".") or (len(forbidden) > 3 and forbidden in name) for name in names), forbidden


def test_no_clock_network_float_or_random_calls() -> None:
    attrs = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert not attrs & {"now", "today", "utcnow", "fromtimestamp", "time", "sleep", "float", "random", "getcontext"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and type(n.value) is float]


_FRAGMENTS = ("provider", "scraper", "requests", "httpx", "browser", "selenium", "playwright", "openai", "gemini", "claude",
              "langchain", "langgraph", "prompt", "sentiment", "score", "rank", "recommend", "severity", "urgency", "thesis",
              "quarantine", "portfolio", "allocation", "rebalance", "broker", "execution", "database", "repository", "supabase",
              "clock", "materiality", "polarity", "bullish", "bearish", "impact", "positive", "negative")
_EXACT = {"llm", "kap", "now", "today", "lot", "order", "tax", "random", "buy", "sell", "hold", "trade"}


def test_no_sentiment_materiality_quarantine_llm_or_portfolio_surface() -> None:
    identifiers: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            identifiers.add(node.name)
        elif isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, ast.arg):
            identifiers.add(node.arg)
    for identifier in identifiers - {"CAPITAL_ALLOCATION"}:                 # spec-mandated structural event type, not a portfolio surface
        lowered = identifier.lower()
        assert lowered not in _EXACT, identifier
        for fragment in _FRAGMENTS:
            assert fragment not in lowered, f"{identifier} contains forbidden surface {fragment!r}"
    members = {m.name.lower() for enum in (GameChangerEventScope, GameChangerEventType, GameChangerRevisionKind) for m in enum}
    assert not members & {"positive", "negative", "bullish", "bearish", "buy", "sell", "hold", "severity", "urgent"}
    function = next(n for n in _TREE.body if isinstance(n, ast.FunctionDef) and n.name == "bind_game_changer_event_pit")
    assert [a.arg for a in function.args.kwonlyargs] == ["event", "context"] and not function.args.args


def test_documents_the_evidence_only_boundary() -> None:
    doc = module_under_test.__doc__ or ""
    for needle in ("evidence", "published", "observed", "effective", "SOURCE_AS_OF", "SYSTEM_AS_OF", "not materiality",
                   "no sentiment", "no LLM", "no quarantine", "source-neutral", "revision"):
        assert needle in doc, needle
