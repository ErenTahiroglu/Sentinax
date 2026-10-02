"""
backend/tests/test_backtest_market_data_bridge.py
=================================================
Phase 26A2: the canonical non-pure bridge from the pure Phase 26A1 replay point to the market-data resolver's temporal parameters. Exact enum-member mapping
(SOURCE_AS_OF to SOURCE_AS_OF, SYSTEM_AS_OF to SYSTEM_AS_OF), the original cutoff object as `as_of`, CURRENT_REPORTED impossible, no fallback and no resolver
call inside the bridge. The existing resolver is exercised only as a consumer.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from backend.engine.private import backtest_market_data_bridge as module_under_test
from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.backtest_market_data_bridge import (
    PrivateBacktestMarketDataContext,
    build_private_backtest_market_data_context,
)
from backend.engine.private.backtest_replay_point import PrivateBacktestReplayPoint, build_private_backtest_replay_point
from backend.engine.private.domain import AsOfMode
from backend.engine.private.market_data import (
    BISTInstrumentQueryKey,
    MarketDataResolutionMode,
    MarketDataResolutionStatus,
    PointInTimeMarketDataResolver,
)
from backend.tests.invariants import static_guards as sg
from backend.tests.test_market_data_resolver import create_mock_bist_obs, create_mock_bist_snapshot

UTC = timezone.utc
PLUS3 = timezone(timedelta(hours=3))
SO, SY = AsOfMode.SOURCE_AS_OF, AsOfMode.SYSTEM_AS_OF
TRADE_DATE = date(2026, 9, 8)


def replay(mode=SY, cutoff=datetime(2026, 9, 10, 10, 0, tzinfo=PLUS3), evaluation_date=date(2026, 9, 9)) -> PrivateBacktestReplayPoint:
    return build_private_backtest_replay_point(pit_context=AnalysisPITContext(mode=mode, knowledge_cutoff=cutoff), evaluation_date=evaluation_date)


def bridge(**kw) -> PrivateBacktestMarketDataContext:
    return build_private_backtest_market_data_context(replay_point=replay(**kw))


# --- contract ------------------------------------------------------------------------------------------------------

def test_stored_shape_is_exactly_the_replay_point() -> None:
    fields = dataclasses.fields(PrivateBacktestMarketDataContext)
    assert [f.name for f in fields] == ["replay_point"]
    assert fields[0].default is dataclasses.MISSING and fields[0].default_factory is dataclasses.MISSING
    assert PrivateBacktestMarketDataContext.__dataclass_params__.frozen is True
    context = bridge()
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.replay_point = replay()  # type: ignore[misc]
    for derived in ("resolution_mode", "as_of", "evaluation_date", "knowledge_cutoff", "mode"):
        assert derived not in context.__dict__ and derived not in {f.name for f in fields}


def test_builder_signature_and_replay_point_identity() -> None:
    parameters = list(inspect.signature(build_private_backtest_market_data_context).parameters.values())
    assert [p.name for p in parameters] == ["replay_point"]
    assert parameters[0].kind is inspect.Parameter.KEYWORD_ONLY and parameters[0].default is inspect.Parameter.empty
    point = replay()
    for extra in ("mode", "as_of", "evaluation_date"):
        with pytest.raises(TypeError):
            build_private_backtest_market_data_context(replay_point=point, **{extra: 1})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_private_backtest_market_data_context(point)  # type: ignore[misc]
    assert build_private_backtest_market_data_context(replay_point=point).replay_point is point


def test_exact_replay_point_type_in_builder_and_direct_construction() -> None:
    class Sub(PrivateBacktestReplayPoint):
        pass

    point = replay()
    sub = Sub(pit_context=point.pit_context, evaluation_date=point.evaluation_date)
    for bad in (None, object(), sub, point.pit_context, "point"):
        with pytest.raises(TypeError):
            build_private_backtest_market_data_context(replay_point=bad)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            PrivateBacktestMarketDataContext(replay_point=bad)  # type: ignore[arg-type]


# --- mapping -------------------------------------------------------------------------------------------------------

def test_source_and_system_mapping_use_enum_identity() -> None:
    assert bridge(mode=SO).resolution_mode is MarketDataResolutionMode.SOURCE_AS_OF
    assert bridge(mode=SY).resolution_mode is MarketDataResolutionMode.SYSTEM_AS_OF


def test_current_reported_is_never_produced() -> None:
    outputs = {bridge(mode=mode).resolution_mode for mode in AsOfMode}
    assert outputs == {MarketDataResolutionMode.SOURCE_AS_OF, MarketDataResolutionMode.SYSTEM_AS_OF}
    assert MarketDataResolutionMode.CURRENT_REPORTED not in outputs
    assert {m.name for m in AsOfMode} == {"SOURCE_AS_OF", "SYSTEM_AS_OF"}                      # the two canonical enums differ in serialized value casing
    assert SO.value != MarketDataResolutionMode.SOURCE_AS_OF.value


def test_as_of_is_the_original_cutoff_object_and_evaluation_date_does_not_change_it() -> None:
    cutoff = datetime(2026, 9, 10, 10, 0, tzinfo=PLUS3)
    context = bridge(cutoff=cutoff)
    assert context.as_of is context.replay_point.knowledge_cutoff is cutoff and context.as_of.utcoffset() == timedelta(hours=3)
    context_a = bridge(cutoff=cutoff, evaluation_date=date(2026, 9, 1))
    context_b = bridge(cutoff=cutoff, evaluation_date=date(2026, 12, 31))             # a later evaluation date is allowed and not a knowledge frontier
    assert context_a.resolution_mode is context_b.resolution_mode and context_a.as_of is context_b.as_of is cutoff
    assert context_a.replay_point != context_b.replay_point


# --- composition with the existing resolver (consumer only) --------------------------------------------------------

def bist_fixture():
    inst = uuid4()
    key = BISTInstrumentQueryKey(instrument_id=inst, trade_date=TRADE_DATE, symbol="THYAO")
    t0, t2 = datetime(2026, 9, 8, 10, 0, tzinfo=UTC), datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    a = create_mock_bist_snapshot(TRADE_DATE, t0, "hash_a", [create_mock_bist_obs("THYAO", inst, TRADE_DATE, Decimal("100.00"))])
    b = create_mock_bist_snapshot(TRADE_DATE, t2, "hash_b", [create_mock_bist_obs("THYAO", inst, TRADE_DATE, Decimal("101.00"))])
    return key, [a, b]


def resolve(context: PrivateBacktestMarketDataContext, key, snapshots):
    return PointInTimeMarketDataResolver.resolve_bist_eod(key, snapshots, context.resolution_mode, as_of=context.as_of)


def test_source_as_of_unavailability_is_preserved_without_fallback() -> None:
    key, snapshots = bist_fixture()
    context = bridge(mode=SO, cutoff=datetime(2026, 9, 8, 23, 0, tzinfo=UTC), evaluation_date=TRADE_DATE)
    result = resolve(context, key, snapshots)
    assert result.status is MarketDataResolutionStatus.UNAVAILABLE_SOURCE_AS_OF and result.selected_observation is None
    assert result.resolution_mode is MarketDataResolutionMode.SOURCE_AS_OF
    assert context.resolution_mode is MarketDataResolutionMode.SOURCE_AS_OF                          # the bridge itself never retries as another mode


def test_system_as_of_selects_only_snapshots_known_at_the_cutoff() -> None:
    key, snapshots = bist_fixture()
    t1 = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)                                                    # T0 10:00 < T1 12:00 < T2 14:00
    result = resolve(bridge(mode=SY, cutoff=t1, evaluation_date=TRADE_DATE), key, snapshots)
    assert result.status is MarketDataResolutionStatus.SELECTED and result.selected_observation.close == Decimal("100.00")
    assert result.resolution_mode is MarketDataResolutionMode.SYSTEM_AS_OF and result.as_of is t1
    later = resolve(bridge(mode=SY, cutoff=datetime(2026, 9, 8, 15, 0, tzinfo=UTC), evaluation_date=TRADE_DATE), key, snapshots)
    assert later.selected_observation.close == Decimal("101.00")


def test_no_historical_snapshot_stays_no_snapshot_as_of() -> None:
    key, snapshots = bist_fixture()
    result = resolve(bridge(mode=SY, cutoff=datetime(2026, 9, 8, 9, 0, tzinfo=UTC), evaluation_date=TRADE_DATE), key, snapshots)
    assert result.status is MarketDataResolutionStatus.NO_SNAPSHOT_AS_OF and result.selected_observation is None
    assert result.resolution_mode is MarketDataResolutionMode.SYSTEM_AS_OF                            # never re-run as CURRENT_REPORTED


def test_non_utc_cutoff_is_compared_by_instant_and_the_object_is_preserved() -> None:
    key, snapshots = bist_fixture()
    cutoff = datetime(2026, 9, 8, 15, 0, tzinfo=PLUS3)                                              # 12:00 UTC: between T0 and T2
    context = bridge(mode=SY, cutoff=cutoff, evaluation_date=TRADE_DATE)
    result = resolve(context, key, snapshots)
    assert result.selected_observation.close == Decimal("100.00") and result.as_of is cutoff and context.as_of.tzinfo is PLUS3
    edge = bridge(mode=SY, cutoff=datetime(2026, 9, 8, 13, 0, tzinfo=PLUS3), evaluation_date=TRADE_DATE)           # exactly T0 (10:00 UTC)
    assert resolve(edge, key, snapshots).selected_observation.close == Decimal("100.00")


# --- source-level invariants ---------------------------------------------------------------------------------------

_PATH = Path(module_under_test.__file__)
_SOURCE = _PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/backtest_market_data_bridge.py"


def test_module_is_deliberately_outside_the_pure_manifest() -> None:
    assert _REL not in sg.PURE_MANIFEST


def test_imports_are_only_the_permitted_private_modules() -> None:
    imported = {(n.module, a.name) for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) for a in n.names if (n.module or "").startswith("backend")}
    assert imported == {("backend.engine.private.backtest_replay_point", "PrivateBacktestReplayPoint"), ("backend.engine.private.domain", "AsOfMode"),
                        ("backend.engine.private.market_data.models", "MarketDataResolutionMode")}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, ast.Import)]


def test_mapping_uses_enum_members_directly_never_values_names_or_case() -> None:
    attributes = {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert not attributes & {"value", "name", "upper", "lower", "casefold", "title"}
    assert {"SOURCE_AS_OF", "SYSTEM_AS_OF"} <= attributes


def test_no_current_reported_resolver_query_key_clock_hash_random_io_or_loop() -> None:
    names = ({n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
             | {a.name for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) for a in n.names})
    assert "CURRENT_REPORTED" not in names
    assert not names & {"PointInTimeMarketDataResolver", "resolve_bist_eod", "BISTInstrumentQueryKey", "TefasFundPriceQueryKey", "GlobalEODQueryKey",
                        "PreciousMetalSemanticKey", "now", "utcnow", "today", "time", "uuid4", "random", "secrets", "urandom", "sha256", "hashlib", "open",
                        "client", "rpc", "table", "latest", "current"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.For, ast.While, ast.AsyncFor, ast.AsyncFunctionDef, ast.Await, ast.Try))]
    for node in ast.walk(_TREE):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert not any(part in module.split(".") for part in ("resolver", "providers", "scheduler", "portfolio", "macro", "orchestrator"))
            assert "game_changer" not in module


def test_documentation_states_the_bridge_boundary() -> None:
    doc = (Path(__file__).resolve().parents[2] / "docs" / "PRIVATE_BACKTEST_MARKET_DATA_BRIDGE.md").read_text(encoding="utf-8")
    for needle in ("PURE", "NON-PURE", "market_data/models.py", "SOURCE_AS_OF", "SYSTEM_AS_OF", "CURRENT_REPORTED", "no fallback", "evaluation_date", "as_of"):
        assert needle in doc, needle
