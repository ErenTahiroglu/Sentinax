"""
backend/tests/test_backtest_candidate_eligibility_replay.py
===========================================================
Phase 26D2: conditional historical candidate-universe eligibility replay. A COMPLETE Phase 26C2E bundle whose declared contract is exactly portfolio history plus
candidate-universe slots is combined with EXPLICIT fixed sleeves (replay policy parameters, not historical strategy configuration) and delegated, once, to the closed
Phase 22B `bind_candidate_universes_to_sleeves`. C2E key order is realigned to sleeve AssetClass order by exact identity; non-SELECTED historical states fail closed
through the closed authority; there is no second membership policy, no composition, rebalance, Game Changer, market data or optimizer.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import inspect
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.engine.private import backtest_candidate_eligibility_replay as module_under_test
from backend.engine.private.allocation_candidate_universe import (
    CandidateUniverseCoverage,
    CandidateUniverseQuery,
    CandidateUniverseResolutionStatus as CS,
    CandidateUniverseSleeveBinding,
    bind_candidate_universes_to_sleeves,
    resolve_candidate_universe,
)
from backend.engine.private.allocation_universe_composition import CrossAssetSleeve
from backend.engine.private.backtest_candidate_eligibility_replay import (
    PrivateBacktestCandidateEligibilityReplay,
    replay_private_backtest_candidate_eligibility,
)
from backend.engine.private.backtest_input_bindings import bind_private_backtest_candidate_universe_input
from backend.engine.private.backtest_input_completeness import (
    PrivateBacktestInputCompletenessStatus,
    PrivateBacktestInputKind as K,
    PrivateBacktestInputRequirement as R,
)
from backend.engine.private.domain import AssetClass
from backend.tests.invariants import static_guards as sg
from backend.tests.test_allocation_candidate_universe import SRC, UA, UB, UC, UNI, snap
from backend.tests.test_backtest_input_bindings import analysis
from backend.tests.test_backtest_input_completeness import gc, macro, make, market, only_portfolio, risk, views

UTC = timezone.utc
EQ, FD, CM = AssetClass.EQUITY, AssetClass.FUND, AssetClass.COMMODITY
PUBLISHED, OBSERVED = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)


def member_snapshot(asset, ids, *, source=SRC, universe=UNI, coverage=CandidateUniverseCoverage.COMPLETE_MEMBERSHIP, sha="a" * 64, **over):
    kw = dict(effective_from=date(2026, 1, 1), published=PUBLISHED, observed=OBSERVED, sha=sha, coverage=coverage, asset_class=asset, source=source, universe=universe)
    kw.update(over)
    return snap(ids, **kw)


def universe(context, asset, ids=(UA,), *, source=SRC, universe_key=UNI, snapshots=None, evaluation_date=None, pit=None, **snap_over):
    """A C2A candidate binding. `snapshots=None` builds one SELECTED member snapshot; the query's own source/universe/asset define the C2E key."""
    snaps = (member_snapshot(asset, ids, source=source, universe=universe_key, **snap_over),) if snapshots is None else tuple(snapshots)
    query = CandidateUniverseQuery(source_key=source, universe_key=universe_key, asset_class=asset,
                                   evaluation_date=evaluation_date or context.replay_point.evaluation_date, pit_context=pit or context.pit_context)
    resolution = resolve_candidate_universe(query=query, snapshots=snaps)
    return bind_private_backtest_candidate_universe_input(analysis_context=context, resolution=resolution) if (pit is None and evaluation_date is None) else resolution


def req(binding) -> R:
    query = binding.resolution.query
    return R(K.CANDIDATE_UNIVERSE, f"{query.source_key}|{query.universe_key}|{query.asset_class.value}")


def sleeve(asset, ids=(UA,)) -> CrossAssetSleeve:
    weight = Decimal(1) / Decimal(len(ids))
    return CrossAssetSleeve(asset_class=asset, target_weight=Decimal("0.5"), instrument_ids=tuple(ids), instrument_weights=tuple(weight for _ in ids))


def d2_bundle(context, *bindings):
    pf, preq = only_portfolio(context)
    return make(context, (preq,) + tuple(req(b) for b in bindings), portfolio_history=pf, candidate_universes=tuple(bindings))


def replay(bundle, sleeves):
    return replay_private_backtest_candidate_eligibility(input_bundle=bundle, sleeves=sleeves)


def direct(bundle, sleeves, binding):
    return PrivateBacktestCandidateEligibilityReplay(input_bundle=bundle, sleeves=sleeves, eligibility_binding=binding)


def simple():
    context = analysis()
    binding = universe(context, EQ, (UA,))
    return context, binding, d2_bundle(context, binding), (sleeve(EQ, (UA,)),)


# --- A, B, C-E: contract and exact types -----------------------------------------------------------------------------

def test_result_is_exactly_three_frozen_fields_without_defaults() -> None:
    fs = dataclasses.fields(PrivateBacktestCandidateEligibilityReplay)
    assert [f.name for f in fs] == ["input_bundle", "sleeves", "eligibility_binding"]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fs)
    assert PrivateBacktestCandidateEligibilityReplay.__dataclass_params__.frozen is True
    _, _, bundle, sleeves = simple()
    result = replay(bundle, sleeves)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.sleeves = ()  # type: ignore[misc]


def test_builder_has_exactly_two_keyword_only_parameters_without_defaults() -> None:
    params = inspect.signature(replay_private_backtest_candidate_eligibility).parameters
    assert list(params) == ["input_bundle", "sleeves"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty for p in params.values())
    _, _, bundle, sleeves = simple()
    with pytest.raises(TypeError):
        replay_private_backtest_candidate_eligibility(bundle, sleeves)  # type: ignore[misc]


def test_exact_type_discipline_for_bundle_sleeves_and_members() -> None:
    _, _, bundle, sleeves = simple()

    class SubBundle(type(bundle)):
        pass

    class SubTuple(tuple):
        pass

    class SubSleeve(CrossAssetSleeve):
        pass

    sub_bundle = SubBundle(**{f.name: getattr(bundle, f.name) for f in dataclasses.fields(bundle)})
    sub_sleeve = SubSleeve(asset_class=EQ, target_weight=Decimal("0.5"), instrument_ids=(UA,), instrument_weights=(Decimal(1),))
    for bad in (sub_bundle, object(), None):
        with pytest.raises(TypeError):
            replay(bad, sleeves)
    for bad in (list(sleeves), set(sleeves), iter(sleeves), SubTuple(sleeves), (sub_sleeve,), (object(),), None):
        with pytest.raises(TypeError):
            replay(bundle, bad)  # type: ignore[arg-type]


# --- F, G: COMPLETE admission ----------------------------------------------------------------------------------------

def test_incomplete_bundle_is_rejected_and_complete_status_cannot_be_forged() -> None:
    context = analysis()
    pf, preq = only_portfolio(context)
    binding = universe(context, EQ)
    incomplete = make(context, (preq, req(binding), R(K.CANDIDATE_UNIVERSE, f"{SRC}|{UNI}|fund")), portfolio_history=pf, candidate_universes=(binding,))
    assert incomplete.status is PrivateBacktestInputCompletenessStatus.INCOMPLETE
    with pytest.raises(ValueError):
        replay(incomplete, (sleeve(EQ),))
    forged = copy.copy(incomplete)
    object.__setattr__(forged, "status", PrivateBacktestInputCompletenessStatus.COMPLETE)
    assert forged.missing_requirements != ()
    with pytest.raises(ValueError):
        replay(forged, (sleeve(EQ),))


# --- H-N: exact D2 manifest surface ----------------------------------------------------------------------------------

def test_portfolio_plus_candidate_universe_manifest_is_accepted() -> None:
    _, _, bundle, sleeves = simple()
    assert bundle.status is PrivateBacktestInputCompletenessStatus.COMPLETE
    assert isinstance(replay(bundle, sleeves).eligibility_binding, CandidateUniverseSleeveBinding)


@pytest.mark.parametrize("kind", [K.GAME_CHANGER, K.MACRO, K.RISK_EVIDENCE, K.MARKET_DATA, K.USER_VIEWS])
def test_any_other_requirement_kind_is_rejected_even_when_complete(kind) -> None:
    context = analysis()
    pf, preq = only_portfolio(context)
    binding = universe(context, EQ)
    extra = {}
    if kind is K.GAME_CHANGER:
        evidence = gc(context)
        requirement, extra = R(K.GAME_CHANGER, "c" * 64), {"game_changers": (evidence,)}
    elif kind is K.MACRO:
        requirement, extra = R(K.MACRO, "TR_FX_USDTRY"), {"macro_inputs": (macro(context),)}
    elif kind is K.RISK_EVIDENCE:
        requirement, extra = R(K.RISK_EVIDENCE, "cash_balance"), {"risk_evidence": (risk(context),)}
    elif kind is K.MARKET_DATA:
        snapshot, key, _, _ = market(context)
        requirement, extra = R(K.MARKET_DATA, f"{snapshot.kind.value}|{key.to_string()}"), {"market_data": (snapshot,)}
    else:
        requirement, extra = R(K.USER_VIEWS, "user_views"), {"user_views": views(context)}
    bundle = make(context, (preq, req(binding), requirement), portfolio_history=pf, candidate_universes=(binding,), **extra)
    assert bundle.status is PrivateBacktestInputCompletenessStatus.COMPLETE
    with pytest.raises(ValueError):
        replay(bundle, (sleeve(EQ),))


def test_zero_candidate_universe_requirements_are_rejected() -> None:
    context = analysis()
    pf, preq = only_portfolio(context)
    bundle = make(context, (preq,), portfolio_history=pf)
    assert bundle.status is PrivateBacktestInputCompletenessStatus.COMPLETE
    with pytest.raises(ValueError):
        replay(bundle, (sleeve(EQ),))


def test_zero_sleeves_are_rejected() -> None:
    _, _, bundle, _ = simple()
    with pytest.raises((TypeError, ValueError)):
        replay(bundle, ())


# --- P-S: success, multiple sleeves, realignment and identity --------------------------------------------------------

def test_one_sleeve_and_one_matching_universe_succeeds_with_exact_identities() -> None:
    _, binding, bundle, sleeves = simple()
    result = replay(bundle, sleeves)
    assert result.input_bundle is bundle and result.sleeves is sleeves and result.sleeves[0] is sleeves[0]
    assert result.eligibility_binding.sleeves is sleeves and result.eligibility_binding.universes[0] is binding.resolution
    assert result.eligibility_binding == bind_candidate_universes_to_sleeves(sleeves=sleeves, universes=(binding.resolution,))


def test_multiple_sleeves_succeed_in_canonical_asset_class_order() -> None:
    context = analysis()
    cm, eq, fd = universe(context, CM, (UC,)), universe(context, EQ, (UA,)), universe(context, FD, (UB,))
    bundle = d2_bundle(context, fd, cm, eq)
    sleeves = (sleeve(CM, (UC,)), sleeve(EQ, (UA,)), sleeve(FD, (UB,)))
    result = replay(bundle, sleeves)
    assert [u.query.asset_class for u in result.eligibility_binding.universes] == [CM, EQ, FD]


def test_c2e_key_order_that_differs_from_asset_class_order_is_realigned_by_identity() -> None:
    context = analysis()
    equity = universe(context, EQ, (UA,), universe_key="alpha-universe")                 # C2E key sorts first ...
    commodity = universe(context, CM, (UC,), universe_key="zeta-universe")               # ... but AssetClass order is commodity, equity
    bundle = d2_bundle(context, equity, commodity)
    assert [b.resolution.query.asset_class for b in bundle.candidate_universes] == [EQ, CM]
    sleeves = (sleeve(CM, (UC,)), sleeve(EQ, (UA,)))
    result = replay(bundle, sleeves)
    universes = result.eligibility_binding.universes
    assert universes[0] is commodity.resolution and universes[1] is equity.resolution
    assert all(sl is sp for sl, sp in zip(result.eligibility_binding.sleeves, sleeves))
    assert bundle.candidate_universes[0] is equity                                        # the bundle itself is not reordered
    with pytest.raises(ValueError):                                                       # passing the C2E tuple blindly would misalign: the closed binder refuses
        bind_candidate_universes_to_sleeves(sleeves=sleeves, universes=tuple(b.resolution for b in bundle.candidate_universes))


# --- T-V: one candidate universe per sleeve --------------------------------------------------------------------------

def test_missing_universe_for_a_sleeve_is_rejected() -> None:
    context = analysis()
    bundle = d2_bundle(context, universe(context, EQ))
    with pytest.raises(ValueError):
        replay(bundle, (sleeve(EQ), sleeve(FD, (UB,))))


def test_two_universes_for_the_same_sleeve_asset_class_are_rejected_without_a_tiebreak() -> None:
    context = analysis()
    first = universe(context, EQ, (UA,), universe_key="alpha-universe")
    second = universe(context, EQ, (UA,), universe_key="beta-universe")
    bundle = d2_bundle(context, first, second)
    assert len(bundle.candidate_universes) == 2
    with pytest.raises(ValueError):
        replay(bundle, (sleeve(EQ),))


def test_universe_for_an_undeclared_sleeve_asset_class_is_rejected() -> None:
    context = analysis()
    bundle = d2_bundle(context, universe(context, EQ), universe(context, FD, (UB,)))
    with pytest.raises(ValueError):
        replay(bundle, (sleeve(EQ),))


def test_noncanonical_or_duplicate_sleeve_configuration_fails_through_the_closed_authority() -> None:
    context = analysis()
    bundle = d2_bundle(context, universe(context, EQ), universe(context, FD, (UB,)))
    with pytest.raises(ValueError):
        replay(bundle, (sleeve(FD, (UB,)), sleeve(EQ)))                                    # not ascending: not sorted by D2
    with pytest.raises(ValueError):
        replay(bundle, (sleeve(EQ), sleeve(EQ)))


# --- W-Y: membership --------------------------------------------------------------------------------------------------

def test_selected_membership_succeeds_and_missing_candidate_fails_closed() -> None:
    context = analysis()
    ok = d2_bundle(context, universe(context, EQ, (UA, UB)))
    assert replay(ok, (sleeve(EQ, (UA, UB)),)).eligibility_binding.universes[0].status is CS.SELECTED
    assert replay(ok, (sleeve(EQ, (UA,)),))                                                # a subset of the selected members is fine
    with pytest.raises(ValueError):
        replay(ok, (sleeve(EQ, (UA, UC)),))                                                # UC is not a member
    context = analysis()
    with pytest.raises(ValueError):
        replay(d2_bundle(context, universe(context, EQ, (UB,))), (sleeve(EQ, (UA,)),))


def test_selected_empty_universe_with_a_nonempty_sleeve_fails_and_is_not_treated_as_missing() -> None:
    context = analysis()
    empty = universe(context, EQ, ())
    assert empty.resolution.status is CS.SELECTED and empty.resolution.instrument_ids == ()
    bundle = d2_bundle(context, empty)
    assert bundle.status is PrivateBacktestInputCompletenessStatus.COMPLETE
    with pytest.raises(ValueError):
        replay(bundle, (sleeve(EQ, (UA,)),))


# --- Z-AD: non-SELECTED historical states fail closed -----------------------------------------------------------------

def unavailable_cases(context):
    late = member_snapshot(EQ, (UA,), published=datetime(2026, 9, 1, tzinfo=UTC), observed=datetime(2026, 9, 2, tzinfo=UTC), sha="b" * 64)
    clash_a = member_snapshot(EQ, (UA,), sha="c" * 64)
    clash_b = member_snapshot(EQ, (UA, UB), sha="d" * 64)
    future_only = member_snapshot(EQ, (UA,), effective_from=date(2027, 1, 1), sha="e" * 64)
    other_source = member_snapshot(EQ, (UA,), source="other-source")
    return {CS.NO_SOURCE_SNAPSHOT: (other_source,), CS.NO_EFFECTIVE_SNAPSHOT: (future_only,), CS.NO_SNAPSHOT_AS_OF: (late,), CS.FRONTIER_CONFLICT: (clash_a, clash_b)}


@pytest.mark.parametrize("status", [CS.NO_SOURCE_SNAPSHOT, CS.NO_EFFECTIVE_SNAPSHOT, CS.NO_SNAPSHOT_AS_OF, CS.FRONTIER_CONFLICT])
def test_present_but_unavailable_or_conflicted_states_are_complete_for_c2e_and_fail_closed_here(status) -> None:
    context = analysis()
    binding = universe(context, EQ, snapshots=unavailable_cases(context)[status])
    assert binding.resolution.status is status
    bundle = d2_bundle(context, binding)
    assert bundle.status is PrivateBacktestInputCompletenessStatus.COMPLETE            # C2E counts the state as present evidence
    with pytest.raises(ValueError):
        replay(bundle, (sleeve(EQ, (UA,)),))                                           # but it cannot establish eligibility, and never becomes empty / a fallback


def test_no_fallback_to_another_snapshot_or_current_state() -> None:
    context = analysis()
    late = member_snapshot(EQ, (UA,), published=datetime(2026, 9, 1, tzinfo=UTC), observed=datetime(2026, 9, 2, tzinfo=UTC))
    binding = universe(context, EQ, snapshots=(late,))
    assert binding.resolution.status is CS.NO_SNAPSHOT_AS_OF and binding.resolution.snapshots == (late,)
    with pytest.raises(ValueError):
        replay(d2_bundle(context, binding), (sleeve(EQ),))


# --- AE, AF: coverage claims are not strengthened ---------------------------------------------------------------------

@pytest.mark.parametrize("coverage", list(CandidateUniverseCoverage))
def test_curated_and_complete_membership_inclusion_succeed_without_a_stronger_claim(coverage) -> None:
    context = analysis()
    binding = universe(context, EQ, (UA, UB), coverage=coverage)
    result = replay(d2_bundle(context, binding), (sleeve(EQ, (UA,)),))
    assert result.eligibility_binding.universes[0] is binding.resolution
    assert {f.name for f in dataclasses.fields(result)} == {"input_bundle", "sleeves", "eligibility_binding"}    # no completeness / eligibility-of-others field
    curated = universe(analysis(), EQ, (UB,), coverage=CandidateUniverseCoverage.CURATED_CANDIDATES)
    with pytest.raises(ValueError):                                                    # absence from a curated set is not eligibility
        replay(d2_bundle(curated.analysis_context, curated), (sleeve(EQ, (UA,)),))


# --- AG-AI: context and temporal authority ----------------------------------------------------------------------------

def forged_bundle(bundle, binding_like):
    forged = copy.copy(bundle)
    object.__setattr__(forged, "candidate_universes", (binding_like,))
    return forged


def test_context_pit_and_evaluation_date_mismatches_are_rejected() -> None:
    context, binding, bundle, sleeves = simple()
    clone = type(context)(replay_point=context.replay_point, temporal_context=context.temporal_context)
    assert clone == context and clone is not context
    with pytest.raises(ValueError):
        replay(forged_bundle(bundle, SimpleNamespace(analysis_context=clone, resolution=binding.resolution)), sleeves)
    from backend.engine.private.analysis_pit import AnalysisPITContext
    equal_pit = AnalysisPITContext(mode=context.pit_context.mode, knowledge_cutoff=context.pit_context.knowledge_cutoff)
    foreign_pit = universe(context, EQ, pit=equal_pit)
    assert foreign_pit.query.pit_context == context.pit_context and foreign_pit.query.pit_context is not context.pit_context
    with pytest.raises(ValueError):
        replay(forged_bundle(bundle, SimpleNamespace(analysis_context=context, resolution=foreign_pit)), sleeves)
    wrong_date = universe(context, EQ, evaluation_date=date(2026, 6, 28))
    with pytest.raises(ValueError):
        replay(forged_bundle(bundle, SimpleNamespace(analysis_context=context, resolution=wrong_date)), sleeves)


# --- AJ-AM, AN-AO: direct construction ---------------------------------------------------------------------------------

def test_direct_construction_accepts_the_builder_composition_and_an_independent_canonical_binding() -> None:
    _, binding, bundle, sleeves = simple()
    built = replay(bundle, sleeves)
    assert direct(bundle, sleeves, built.eligibility_binding) == built
    independent = CandidateUniverseSleeveBinding(sleeves=sleeves, universes=(binding.resolution,))
    assert independent is not built.eligibility_binding and direct(bundle, sleeves, independent) == built


def test_direct_construction_rejects_reordered_cloned_foreign_and_mistyped_compositions() -> None:
    context = analysis()
    commodity, equity = universe(context, CM, (UC,)), universe(context, EQ, (UA,))
    bundle = d2_bundle(context, commodity, equity)
    sleeves = (sleeve(CM, (UC,)), sleeve(EQ, (UA,)))
    good = replay(bundle, sleeves).eligibility_binding
    with pytest.raises(ValueError):
        direct(bundle, sleeves, CandidateUniverseSleeveBinding(sleeves=(sleeves[1], sleeves[0]), universes=(equity.resolution, commodity.resolution)))  # invalid in 22B
    cloned = copy.deepcopy(equity.resolution)
    assert cloned == equity.resolution and cloned is not equity.resolution
    with pytest.raises(ValueError):
        direct(bundle, sleeves, CandidateUniverseSleeveBinding(sleeves=sleeves, universes=(commodity.resolution, cloned)))
    other_context = analysis()
    foreign = universe(other_context, EQ, (UA,))
    with pytest.raises(ValueError):
        direct(bundle, sleeves, CandidateUniverseSleeveBinding(sleeves=sleeves, universes=(commodity.resolution, foreign.resolution)))
    cloned_sleeve = sleeve(EQ, (UA,))
    with pytest.raises(ValueError):
        direct(bundle, sleeves, CandidateUniverseSleeveBinding(sleeves=(sleeves[0], cloned_sleeve), universes=good.universes))
    other = replay(d2_bundle(context, equity), (sleeve(EQ, (UA,)),)).eligibility_binding
    with pytest.raises(ValueError):
        direct(bundle, sleeves, other)
    for bad in (object(), None, good.universes):
        with pytest.raises(TypeError):
            direct(bundle, sleeves, bad)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        direct(object(), sleeves, good)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        direct(bundle, list(sleeves), good)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        direct(forged_status(bundle), sleeves, good)


def forged_status(bundle):
    forged = copy.copy(bundle)
    object.__setattr__(forged, "missing_requirements", (bundle.requirements[0],))
    return forged


# --- AP-AY: scope and determinism -------------------------------------------------------------------------------------

def test_portfolio_history_is_retained_untouched_and_the_binder_is_called_once(monkeypatch) -> None:
    context, binding, bundle, sleeves = simple()
    history, projection = bundle.portfolio_history, bundle.portfolio_history.projection_binding.projection
    calls = []
    real = module_under_test.bind_candidate_universes_to_sleeves

    def spy(*, sleeves, universes):
        calls.append((sleeves, universes))
        return real(sleeves=sleeves, universes=universes)

    monkeypatch.setattr(module_under_test, "bind_candidate_universes_to_sleeves", spy)
    result = replay(bundle, sleeves)
    assert len(calls) == 1 and calls[0][0] is sleeves and calls[0][1][0] is binding.resolution
    assert bundle.portfolio_history is history and history.projection_binding.projection is projection and result.input_bundle.portfolio_history is history


def test_same_bundle_and_sleeves_give_an_equivalent_deterministic_result() -> None:
    _, _, bundle, sleeves = simple()
    first, second = replay(bundle, sleeves), replay(bundle, sleeves)
    assert first == second and first.eligibility_binding.universes[0] is second.eligibility_binding.universes[0]


def test_result_carries_no_weight_rank_composition_rebalance_or_decision_surface() -> None:
    _, _, bundle, sleeves = simple()
    result = replay(bundle, sleeves)
    surface = {n.lower() for n in dir(result) if not n.startswith("_")} | {f.name.lower() for f in dataclasses.fields(result.eligibility_binding)}
    for word in ("rank", "score", "rebalance", "plan", "trade", "order", "execut", "gate", "decision", "hash", "status", "normal", "authority"):
        assert not any(word in name for name in surface), word


# --- source-surface guards ---------------------------------------------------------------------------------------------

_PATH = Path(module_under_test.__file__)
_SOURCE = _PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/backtest_candidate_eligibility_replay.py"


def _names() -> set:
    return ({n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
            | {a.name for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) for a in n.names})


def test_only_the_phase_22b_binder_is_an_economic_eligibility_builder() -> None:
    forbidden = {"PointInTimeMarketDataResolver", "PrivateBacktestMarketDataResolutionSnapshot", "build_cross_asset_composition_plan", "CrossUniverseAuthority",
                 "build_cash_first_rebalance_plan", "build_band_aware_rebalance_plan", "RebalanceBandPolicy", "RebalanceFrictionProfile", "RebalanceCurrentState",
                 "RebalanceTargetAllocation", "build_user_return_view_set", "build_bayesian_expected_return_posterior", "optimize_minimum_cvar_portfolio",
                 "ExpectedReturnPrior", "UserReturnViewSet", "AllocationReturnPanel", "AllocationCovarianceMatrix", "GameChangerDecisionGate",
                 "PrivateBacktestGameChangerDecisionReplay", "build_game_changer_decision_gate", "scheduler", "dispatcher", "selected_observation", "resolution_payload_json"}
    assert not _names() & forbidden
    builders = {n for n in _names() if n.startswith(("build_", "resolve_", "optimize_", "bind_"))}
    assert builders == {"bind_candidate_universes_to_sleeves"}


def test_imports_exclude_market_data_and_every_other_decision_module() -> None:
    modules = {n.module for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("backend")}
    assert modules <= {"backend.engine.private.allocation_candidate_universe", "backend.engine.private.allocation_universe_composition",
                       "backend.engine.private.backtest_input_completeness"}
    plain = {a.name for n in ast.walk(_TREE) if isinstance(n, ast.Import) for a in n.names}
    assert not any(any(token in m for token in ("market_data", "backtest_market", "rebalance", "cvar", "scheduler", "game_changer", "bist", "tefas", "precious", "fund_",
                                                "user_view", "bayes"))
                   for m in modules | plain)


def test_membership_policy_is_not_duplicated_and_portfolio_is_not_consumed() -> None:
    assert not _names() & {"instrument_ids", "instrument_weights", "target_weight", "coverage", "selected_snapshot", "snapshots", "transactions", "projection",
                           "known_transactions", "portfolio_history", "holdings", "weights", "normalize", "sorted", "reversed", "max", "min", "sum", "score", "rank",
                           "SELECTED", "NO_SOURCE_SNAPSHOT", "FRONTIER_CONFLICT", "NO_SNAPSHOT_AS_OF", "NO_EFFECTIVE_SNAPSHOT", "CandidateUniverseResolutionStatus",
                           "CandidateUniverseCoverage", "CURATED_CANDIDATES", "COMPLETE_MEMBERSHIP"}
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Attribute) and node.attr == "status":
            assert (isinstance(node.value, ast.Name) and node.value.id == "input_bundle") or (
                isinstance(node.value, ast.Attribute) and node.value.attr == "input_bundle"), ast.dump(node)


def test_no_clock_random_hash_io_or_generated_identity() -> None:
    assert not _names() & {"now", "utcnow", "today", "time", "uuid4", "uuid1", "UUID", "random", "secrets", "urandom", "sha256", "hashlib", "hmac", "hash", "open",
                           "client", "rpc", "table", "environ", "getenv", "sleep", "datetime", "date", "Decimal"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.Global, ast.Nonlocal, ast.AsyncFunctionDef, ast.Await, ast.Try))]


def test_module_is_outside_the_pure_manifest_and_c2c2_is_not_a_dependency() -> None:
    assert _REL not in sg.PURE_MANIFEST
    assert "c2c2" not in _SOURCE.lower()


def test_documentation_and_ci_wiring() -> None:
    root = Path(__file__).resolve().parents[2]
    doc = (root / "docs" / "PRIVATE_BACKTEST_CANDIDATE_ELIGIBILITY_REPLAY.md").read_text(encoding="utf-8")
    for needle in ("second Phase 26D", "COMPLETE", "fixed replay policy", "not claimed", "counterfactual", "AssetClass", "Phase 22B", "never becomes empty", "selected-empty",
                   "CURATED_CANDIDATES", "COMPLETE_MEMBERSHIP", "no cross-asset composition", "no rebalance", "no market data", "C2C2 remains deferred"):
        assert needle in doc, needle
    completeness = (root / "docs" / "PRIVATE_BACKTEST_INPUT_COMPLETENESS.md").read_text(encoding="utf-8")
    assert "Phase 26D2 consumes COMPLETE portfolio + candidate-universe bundles for conditional historical sleeve-eligibility replay" in completeness
    assert "fixed replay policy parameters" in completeness and "C2C2 remains deferred because D2 consumes no typed market observation" in completeness
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    architecture = ci.split("Phase 26 backtest architecture correctness", 1)[1].split("- name:", 1)[0]
    assert "backend/tests/test_backtest_candidate_eligibility_replay.py" in architecture
