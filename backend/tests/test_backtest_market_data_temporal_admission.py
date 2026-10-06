"""
backend/tests/test_backtest_market_data_temporal_admission.py
=============================================================
Phase 27 FIX B (finding F-01): adversarial composition proving that a directly constructed C2C1 SELECTED envelope that the closed resolver could never produce at its own
frontier (a snapshot retrieved AFTER `as_of`, or SELECTED under SOURCE_AS_OF) is retained by C2C1 as an audit object but is refused by C2C2, so it can reach neither D3A marked
holdings nor a completed D6 sequence, while a temporally admissible envelope (including one retrieved exactly AT the cutoff) still traverses the normal D3 -> D6 chain.
No monkeypatching of production code: only the test fixture price-builder table is swapped for the downstream chain. C2C2 never replays the resolver or recomputes the resolution key.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import pytest

from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.backtest_input_completeness import PrivateBacktestInputCompletenessStatus
from backend.engine.private.backtest_market_data_bridge import build_private_backtest_market_data_context
from backend.engine.private.backtest_market_data_resolution_snapshot import (
    PrivateBacktestMarketDataResolutionSnapshot,
    bind_private_backtest_bist_eod_resolution,
)
from backend.engine.private.backtest_market_data_selected_observation import reconstruct_private_backtest_selected_observation
from backend.engine.private.backtest_marked_holdings import build_private_backtest_marked_holdings_state
from backend.engine.private.backtest_replay_point import build_private_backtest_replay_point
from backend.engine.private.domain import AsOfMode, Currency
from backend.engine.private.market_data.models import BISTInstrumentQueryKey, MarketDataResolutionStatus as S
from backend.tests import test_backtest_cross_universe_composition as d4b_fixtures
from backend.tests.test_backtest_decision_replay_sequence import plan_of, sequence_for
from backend.tests.test_backtest_input_bindings import analysis
from backend.tests.test_backtest_marked_holdings import A1, EVAL, I1, OWNER, bundle_for, buy, coverage_for, deposit, mctx
from backend.tests.test_market_data_resolver import create_mock_bist_obs, create_mock_bist_snapshot
from backend.tests.test_portfolio_projection import _make_portfolio

SY, SO = AsOfMode.SYSTEM_AS_OF, AsOfMode.SOURCE_AS_OF
TRY = Currency.TRY
CLOSE = Decimal("999")


def cutoff_of(context):
    return context.replay_point.knowledge_cutoff


def raw_bist(context, retrieved_at, close=CLOSE, inst=I1):
    """A real closed-resolver result for one BIST price whose source snapshot was retrieved at `retrieved_at`, bound under `market_context`."""
    obs = create_mock_bist_obs("THYAO", inst, EVAL, close)
    obs.currency = TRY
    snap = create_mock_bist_snapshot(EVAL, retrieved_at, "h" * 8, [obs])
    key = BISTInstrumentQueryKey(instrument_id=inst, trade_date=EVAL, symbol="THYAO")
    return key, snap


def bind_at(context, retrieved_at, **kw):
    key, snap = raw_bist(context, retrieved_at, **kw)
    return bind_private_backtest_bist_eod_resolution(market_context=mctx(context), query_key=key, snapshots=(snap,)), key, snap


def market_context_at(cutoff, mode=SY):
    point = build_private_backtest_replay_point(pit_context=AnalysisPITContext(mode=mode, knowledge_cutoff=cutoff), evaluation_date=EVAL)
    return build_private_backtest_market_data_context(replay_point=point)


def rebound(genuine, context):
    """The Phase 27 F-01 construction: only the claimed top-level frontier fields change; the genuine selection and its retrieval instant are NOT repaired."""
    payload = genuine.resolution_payload()
    payload["resolution_mode"] = context.resolution_mode.value
    payload["as_of"] = context.as_of.isoformat()
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return PrivateBacktestMarketDataResolutionSnapshot(market_context=context, kind=genuine.kind, query_key=genuine.query_key, resolution_payload_json=text)


def future_envelope(context, inst=I1, close=CLOSE):
    """A genuine SELECTED result under a LATER SYSTEM_AS_OF frontier, rebound (not repaired) to `context`'s earlier frontier."""
    late_cutoff = cutoff_of(context) + timedelta(days=2)
    retrieved = cutoff_of(context) + timedelta(days=1)
    key, snap = raw_bist(context, retrieved, close, inst)
    late = bind_private_backtest_bist_eod_resolution(market_context=market_context_at(late_cutoff), query_key=key, snapshots=(snap,))
    assert late.status is S.SELECTED
    return rebound(late, mctx(context)), retrieved


def boundary_envelope(context, inst=I1, close=CLOSE):
    """A genuine SELECTED result whose source snapshot was retrieved EXACTLY at the replay cutoff (the closed resolver's inclusive boundary)."""
    key, snap = raw_bist(context, cutoff_of(context), close, inst)
    envelope = bind_private_backtest_bist_eod_resolution(market_context=mctx(context), query_key=key, snapshots=(snap,))
    assert envelope.status is S.SELECTED
    return envelope


def standard_world(context):
    pf = _make_portfolio(owner_id=OWNER)
    coverage = coverage_for(context, pf, [deposit(pf, A1, "1000", 0), buy(pf, A1, I1, "10", 1)])
    return pf, coverage


def mark(context, coverage, envelope):
    bundle = bundle_for(context, coverage, envelope)
    return bundle, lambda: build_private_backtest_marked_holdings_state(input_bundle=bundle, valuation_currency=TRY)


# --- P, Q: the Phase 27 reproduction -------------------------------------------------------------------------------------------------------

def test_the_closed_resolver_never_selects_the_future_snapshot_at_the_replay_frontier() -> None:
    context = analysis()
    key, snap = raw_bist(context, cutoff_of(context) + timedelta(days=1))
    honest = bind_private_backtest_bist_eod_resolution(market_context=mctx(context), query_key=key, snapshots=(snap,))
    assert honest.status is S.NO_SNAPSHOT_AS_OF


def test_c2c1_retains_the_future_envelope_as_an_audit_object_but_c2c2_rejects_that_exact_object() -> None:
    context = analysis()
    envelope, retrieved = future_envelope(context)
    assert type(envelope) is PrivateBacktestMarketDataResolutionSnapshot and envelope.status is S.SELECTED        # P: C2C1 still holds the audit object
    assert envelope.resolution_payload()["snapshot_retrieved_at"] == retrieved.isoformat()                         # the future retrieval instant was not repaired
    assert retrieved > cutoff_of(context)
    with pytest.raises(ValueError, match="temporal frontier"):                                                      # Q
        reconstruct_private_backtest_selected_observation(resolution_snapshot=envelope)


def test_source_as_of_selected_is_retained_by_c2c1_and_rejected_by_c2c2() -> None:
    context = analysis()
    genuine = boundary_envelope(context)
    forged = rebound(genuine, market_context_at(cutoff_of(context), SO))
    assert forged.status is S.SELECTED and forged.market_context.resolution_mode.value == "SOURCE_AS_OF"
    with pytest.raises(ValueError, match="temporal frontier"):
        reconstruct_private_backtest_selected_observation(resolution_snapshot=forged)


# --- R, S: D3A -------------------------------------------------------------------------------------------------------------------------------

def test_d3a_cannot_mark_from_the_future_envelope_and_returns_no_marked_position() -> None:
    context = analysis()
    pf, coverage = standard_world(context)
    envelope, _retrieved = future_envelope(context)
    bundle, build = mark(context, coverage, envelope)
    assert bundle.status is PrivateBacktestInputCompletenessStatus.COMPLETE        # C2E completeness alone does not judge the temporal frontier
    with pytest.raises(ValueError, match="temporal frontier"):
        build()


@pytest.mark.parametrize("retrieved_offset", [timedelta(microseconds=1), timedelta(seconds=1), timedelta(days=30)], ids=["1us", "1s", "30d"])
def test_every_future_retrieval_is_rejected_without_tolerance(retrieved_offset) -> None:
    context = analysis()
    pf, coverage = standard_world(context)
    key, snap = raw_bist(context, cutoff_of(context) + retrieved_offset)
    late = bind_private_backtest_bist_eod_resolution(market_context=market_context_at(cutoff_of(context) + retrieved_offset + timedelta(days=1)), query_key=key, snapshots=(snap,))
    _bundle, build = mark(context, coverage, rebound(late, mctx(context)))
    with pytest.raises(ValueError, match="temporal frontier"):
        build()


def test_valid_control_before_the_cutoff_still_marks() -> None:
    context = analysis()
    pf, coverage = standard_world(context)
    envelope, _key, _snap = bind_at(context, cutoff_of(context) - timedelta(hours=1))
    _bundle, build = mark(context, coverage, envelope)
    state = build()
    assert state.marked_positions[0].market_value == CLOSE * Decimal("10")


def test_valid_control_retrieved_exactly_at_the_cutoff_still_marks() -> None:
    context = analysis()
    pf, coverage = standard_world(context)
    _bundle, build = mark(context, coverage, boundary_envelope(context))
    state = build()
    assert state.marked_positions[0].market_value == CLOSE * Decimal("10")
    assert state.marked_positions[0].market_observation.reconstruct().retrieved_at == cutoff_of(context)


def test_d3a_has_no_temporal_logic_of_its_own_and_delegates_to_c2c2() -> None:
    import ast
    from pathlib import Path
    from backend.engine.private import backtest_marked_holdings as d3a
    tree = ast.parse(Path(d3a.__file__).read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {"knowledge_cutoff", "as_of", "resolution_mode", "MarketDataResolutionMode", "snapshot_retrieved_at"}


# --- T, U: D6 composition ----------------------------------------------------------------------------------------------------------------------

def test_the_future_envelope_chain_cannot_construct_a_completed_d6_sequence(monkeypatch) -> None:
    def future_pricer(context, inst=I1, **_kw):
        return future_envelope(context, inst)[0]
    monkeypatch.setitem(d4b_fixtures.PRICERS, I1, future_pricer)                # the test fixture table only: production code is untouched
    plan = plan_of(1, 2)
    with pytest.raises(ValueError, match="temporal frontier"):
        sequence_for(plan)


def test_a_valid_boundary_envelope_still_traverses_d3_to_a_completed_d6_sequence(monkeypatch) -> None:
    def boundary_pricer(context, inst=I1, **_kw):
        return boundary_envelope(context, inst, Decimal("123.456789"))
    monkeypatch.setitem(d4b_fixtures.PRICERS, I1, boundary_pricer)
    plan = plan_of(1, 2, 3)
    admissions, sequence = sequence_for(plan)
    assert len(sequence.admissions) == 3 and sequence.admissions == admissions
    for admission in admissions:
        state = admission.rebalance_replay.cross_universe_composition.rebalance_current_state.investable_cash_selection.marked_holdings_state
        observation = next(p for p in state.marked_positions if p.instrument_id == I1).market_observation.reconstruct()
        assert observation.retrieved_at == admission.rebalance_replay.cross_universe_composition.candidate_eligibility.input_bundle.analysis_context.replay_point.knowledge_cutoff
