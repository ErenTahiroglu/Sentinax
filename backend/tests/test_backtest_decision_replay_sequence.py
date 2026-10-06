"""
backend/tests/test_backtest_decision_replay_sequence.py
=======================================================
Phase 26D6: ordered decision replay sequence. A Phase 26B `PrivateBacktestReplayPlan` is bound to exactly one completed D5B admission per replay point, by positional replay-point OBJECT identity and one
explicit Horizon. This is sequence/provenance closure only: no new decision, no recomputation, no sorting or matching by date, no early stop, no aggregate verdict, no simulated execution or state
propagation, no performance, no cross-point policy-stability claim and no zero-Game-Changer-event inference.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from backend.engine.private import backtest_decision_replay_sequence as module_under_test
from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.backtest_analysis_context import build_private_backtest_analysis_context
from backend.engine.private.backtest_decision_replay_sequence import (
    PrivateBacktestDecisionReplaySequence,
    build_private_backtest_decision_replay_sequence,
)
from backend.engine.private.backtest_game_changer_plan_admission import PrivateBacktestGameChangerPlanAdmission, PrivateBacktestNewCapitalAdmissionState
from backend.engine.private.backtest_replay_plan import PrivateBacktestReplayPlan, build_private_backtest_replay_plan
from backend.engine.private.backtest_replay_point import build_private_backtest_replay_point
from backend.engine.private.domain import AsOfMode, Horizon, HorizonFamily, PortfolioMode
from backend.tests.invariants import static_guards as sg
from backend.tests.test_backtest_cross_universe_composition import A1, TRY, USD, Scenario, bind_portfolio, buy, deposit, make_d2, make_d3c
from backend.tests.test_backtest_game_changer_plan_admission import (
    CASH_ONLY_SPEC,
    HELD,
    I1,
    I2,
    I3,
    MIXED_SPEC,
    OWNER,
    admit,
    authority,
    d1_for,
    d5a_for,
    scenario,
)
from backend.tests.test_portfolio_projection import _make_portfolio

UTC = timezone.utc
SY = AsOfMode.SYSTEM_AS_OF
EVAL = date(2026, 6, 29)                                                    # the D3A price fixtures are dated here: every point may share it (evaluation_date is not order authority)
H12, H1 = Horizon.ALLOCATION_12M, Horizon.TACTICAL_1M
BLOCKED, NOT_BLOCKED = PrivateBacktestNewCapitalAdmissionState.BLOCKED_NEW_CAPITAL, PrivateBacktestNewCapitalAdmissionState.NOT_BLOCKED


# --- fixtures ----------------------------------------------------------------------------------------------------------------------------

def point(day: int, evaluation_date=EVAL, mode=SY):
    pit = AnalysisPITContext(mode=mode, knowledge_cutoff=datetime(2026, 7, day, 12, 0, tzinfo=UTC))
    return build_private_backtest_replay_point(pit_context=pit, evaluation_date=evaluation_date)


def plan_of(*days: int) -> PrivateBacktestReplayPlan:
    return build_private_backtest_replay_plan(points=tuple(point(day) for day in days))


OTHER_OWNER = UUID(int=0xBEEF)
SHARED_PF = _make_portfolio(owner_id=OWNER)                                  # one portfolio (owner, id, MY_PORTFOLIO): the normal case of a single-portfolio replay


def scenario_for(pf, held, spec, *, context, investable="100", cash_try="1000", owner=OWNER, extra=0) -> Scenario:
    """Like the D4B scenario but over a CALLER-SUPPLIED portfolio; `extra` adds later ledger events so the projection content evolves between points."""
    txs = [deposit(pf, A1, "1000000000", 0, USD), deposit(pf, A1, cash_try, 1, TRY)]
    txs += [buy(pf, A1, inst, qty, 2 + index) for index, (inst, qty) in enumerate(held)]
    txs += [deposit(pf, A1, "1", 20 + k, TRY) for k in range(extra)]
    binding = bind_portfolio(context, pf, tuple(txs))
    d3c, selection, state = make_d3c(context, binding, held, owner=owner, investable=investable)
    d2, sleeves = make_d2(context, binding, spec, owner=owner)
    return Scenario(context, pf, txs, binding, d2, d3c, selection, state, sleeves)


def admission_at(replay_point, horizon=H12, gates=(((I2,), "open"),), spec=MIXED_SPEC, held=HELD, auth=None, trigger="0", destination="0", pf=None, owner=OWNER, extra=0):
    """A complete D5B admission whose analysis context is built from THIS very replay point object, over the shared portfolio unless another is supplied."""
    context = build_private_backtest_analysis_context(replay_point=replay_point, horizon=horizon)
    s = scenario_for(pf or SHARED_PF, held, spec, context=context, owner=owner, extra=extra)
    d5a = d5a_for(s, auth if auth is not None else authority(zero=[I2]), trigger=trigger, destination=destination)
    return admit(d1_for(s, *gates, owner=owner), d5a)


def sequence_for(plan, horizon=H12, **kw):
    admissions = tuple(admission_at(p, horizon, **kw) for p in plan.points)
    return admissions, build_private_backtest_decision_replay_sequence(replay_plan=plan, horizon=horizon, admissions=admissions)


def build(plan, horizon, admissions):
    return build_private_backtest_decision_replay_sequence(replay_plan=plan, horizon=horizon, admissions=admissions)


# --- A-M: contract, exact types, identities ------------------------------------------------------------------------------------------------

def test_result_is_exactly_three_frozen_fields_without_defaults() -> None:
    fs = dataclasses.fields(PrivateBacktestDecisionReplaySequence)
    assert [f.name for f in fs] == ["replay_plan", "horizon", "admissions"]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fs)
    assert PrivateBacktestDecisionReplaySequence.__dataclass_params__.frozen is True


def test_builder_is_keyword_only_with_three_parameters_and_no_defaults() -> None:
    params = inspect.signature(build_private_backtest_decision_replay_sequence).parameters
    assert list(params) == ["replay_plan", "horizon", "admissions"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty for p in params.values())
    plan = plan_of(1)
    admissions = (admission_at(plan.points[0]),)
    with pytest.raises(TypeError):
        build_private_backtest_decision_replay_sequence(plan, H12, admissions)  # type: ignore[misc]


def test_exact_types_are_required_for_plan_horizon_tuple_and_members() -> None:
    plan = plan_of(1)
    admissions = (admission_at(plan.points[0]),)

    class SubPlan(PrivateBacktestReplayPlan):
        pass

    class SubTuple(tuple):
        pass

    class SubAdmission(PrivateBacktestGameChangerPlanAdmission):
        pass

    sub_plan = SubPlan(points=plan.points)
    sub_admission = SubAdmission(game_changer_replay=admissions[0].game_changer_replay, rebalance_replay=admissions[0].rebalance_replay)
    for bad in (sub_plan, object(), None, plan.points):
        with pytest.raises(TypeError):
            build(bad, H12, admissions)  # type: ignore[arg-type]
    for bad in ("12M", HorizonFamily.ALLOCATION, None, 12, Horizon.ALLOCATION_12M.value):
        with pytest.raises(TypeError):
            build(plan, bad, admissions)  # type: ignore[arg-type]
    for bad in (list(admissions), set(admissions), iter(admissions), SubTuple(admissions), None, admissions[0]):
        with pytest.raises(TypeError):
            build(plan, H12, bad)  # type: ignore[arg-type]
    for bad in ((sub_admission,), (object(),), (admissions[0].rebalance_replay,), (admissions[0].game_changer_replay,)):        # a raw D5A / D1 result is not a D5B admission
        with pytest.raises(TypeError):
            build(plan, H12, bad)  # type: ignore[arg-type]


def test_plan_tuple_and_every_admission_are_retained_by_identity_without_copying() -> None:
    plan = plan_of(1, 2, 3)
    admissions, sequence = sequence_for(plan)
    assert sequence.replay_plan is plan and sequence.horizon is H12
    assert sequence.admissions is admissions
    assert all(a is b for a, b in zip(sequence.admissions, admissions))
    assert dataclasses.replace(sequence).admissions is admissions


# --- N-W: one result per point, positional object identity ---------------------------------------------------------------------------------

def test_one_point_and_multi_point_sequences_succeed() -> None:
    one = plan_of(1)
    assert len(sequence_for(one)[1].admissions) == 1
    many = plan_of(1, 2, 3)
    assert len(sequence_for(many)[1].admissions) == 3


def test_missing_and_extra_results_are_rejected_without_matching_or_repair() -> None:
    plan = plan_of(1, 2, 3)
    admissions, _ = sequence_for(plan)
    for bad in (admissions[:2], admissions[:1], (), admissions + (admissions[0],), admissions + (admission_at(point(4)),)):
        with pytest.raises(ValueError):
            build(plan, H12, bad)


def test_exact_positional_replay_point_identity_is_required_not_value_equality() -> None:
    plan = plan_of(1, 2)
    clone = build_private_backtest_replay_point(pit_context=AnalysisPITContext(mode=SY, knowledge_cutoff=plan.points[0].pit_context.knowledge_cutoff),
                                                evaluation_date=plan.points[0].evaluation_date)
    assert clone == plan.points[0] and clone is not plan.points[0]
    cloned = admission_at(clone)
    ok_second = admission_at(plan.points[1])
    with pytest.raises(ValueError):
        build(plan, H12, (cloned, ok_second))
    other_replay = admission_at(point(1))                                                      # an equal-valued point from another plan
    with pytest.raises(ValueError):
        build(plan, H12, (other_replay, ok_second))


def test_reversed_and_permuted_order_fails_because_caller_order_is_never_repaired() -> None:
    plan = plan_of(1, 2, 3)
    admissions, _ = sequence_for(plan)
    import itertools
    for permutation in itertools.permutations(admissions):
        if all(a is b for a, b in zip(permutation, admissions)):
            assert build(plan, H12, tuple(permutation)).admissions == tuple(permutation)
        else:
            with pytest.raises(ValueError):
                build(plan, H12, tuple(permutation))


def test_points_with_the_same_evaluation_date_still_follow_plan_position_and_identity() -> None:
    plan = plan_of(1, 2)
    assert plan.points[0].evaluation_date == plan.points[1].evaluation_date == EVAL
    assert plan.points[0].knowledge_cutoff_utc != plan.points[1].knowledge_cutoff_utc
    admissions, sequence = sequence_for(plan)
    assert sequence.admissions[0].game_changer_replay.input_bundle.analysis_context.replay_point is plan.points[0]
    assert sequence.admissions[1].game_changer_replay.input_bundle.analysis_context.replay_point is plan.points[1]
    with pytest.raises(ValueError):
        build(plan, H12, (admissions[1], admissions[0]))                                        # no matching by date


# --- X-Z: explicit horizon -----------------------------------------------------------------------------------------------------------------------

def test_one_explicit_horizon_must_hold_for_every_point() -> None:
    plan = plan_of(1, 2, 3)
    for horizon in (H12, H1):
        admissions, sequence = sequence_for(plan, horizon)
        assert sequence.horizon is horizon
        assert all(a.game_changer_replay.input_bundle.analysis_context.horizon is horizon for a in admissions)
    admissions, _ = sequence_for(plan, H12)
    with pytest.raises(ValueError):
        build(plan, H1, admissions)                                                            # the horizon is never inferred from the results
    mismatched = (admissions[0], admission_at(plan.points[1], H1), admissions[2])
    with pytest.raises(ValueError):
        build(plan, H12, mismatched)
    mixed = (admission_at(plan.points[0], H1), admission_at(plan.points[1], H12), admission_at(plan.points[2], H1))
    for horizon in (H12, H1):
        with pytest.raises(ValueError):
            build(plan, horizon, mixed)


# --- AA-AE: blocked / not-blocked, no early stop, no verdict -----------------------------------------------------------------------------------

def test_blocked_and_not_blocked_admissions_are_both_valid_members_and_preserved() -> None:
    plan = plan_of(1, 2, 3)
    blocked = admission_at(plan.points[0], gates=(((I2,), "quarantined"),))
    not_blocked = admission_at(plan.points[1], gates=(((I2,), "open"),))
    later = admission_at(plan.points[2], gates=(((I3,), "paused"),))
    assert blocked.admission_state is BLOCKED and not_blocked.admission_state is NOT_BLOCKED and later.admission_state is BLOCKED
    sequence = build(plan, H12, (blocked, not_blocked, later))
    assert [a.admission_state for a in sequence.admissions] == [BLOCKED, NOT_BLOCKED, BLOCKED]
    assert sequence.admissions[0] is blocked and sequence.admissions[1] is not_blocked and sequence.admissions[2] is later
    two = plan_of(1, 2)
    follow = build(two, H12, (admission_at(two.points[0], gates=(((I2,), "quarantined"),)), admission_at(two.points[1])))
    assert len(follow.admissions) == 2                                                          # a blocked point never prevents the next one from being present


def test_there_is_no_aggregate_verdict_early_stop_or_performance_surface() -> None:
    plan = plan_of(1, 2)
    _, sequence = sequence_for(plan)
    public = {n for n in dir(sequence) if not n.startswith("_")}
    assert public == {"replay_plan", "horizon", "admissions"}
    banned = ("status", "success", "percent", "worst", "best", "score", "halt", "stop", "terminat", "verdict", "overall", "pass", "fail", "pnl", "profit", "loss",
              "return", "sharpe", "sortino", "drawdown", "turnover", "performance", "benchmark", "alpha", "beta", "hit_rate", "execut", "trade", "fill")
    for word in banned:
        assert not any(word in n.lower() for n in public), word


# --- AF-AM: no execution, no propagation, no policy-stability claim ------------------------------------------------------------------------------

def test_the_sequence_does_not_execute_mutate_or_propagate_state_between_points() -> None:
    plan = plan_of(1, 2)
    admissions = (admission_at(plan.points[0]), admission_at(plan.points[1]))
    before = [(a.rebalance_replay.rebalance_plan, tuple(a.rebalance_replay.rebalance_plan.trades),
               a.game_changer_replay.input_bundle.portfolio_history.projection_binding.projection) for a in admissions]
    sequence = build(plan, H12, admissions)
    after = [(a.rebalance_replay.rebalance_plan, tuple(a.rebalance_replay.rebalance_plan.trades),
              a.game_changer_replay.input_bundle.portfolio_history.projection_binding.projection) for a in sequence.admissions]
    assert all(b[0] is a[0] and b[1] == a[1] and b[2] is a[2] for b, a in zip(before, after))
    first_ledger = admissions[0].game_changer_replay.input_bundle.portfolio_history.projection_binding
    second_ledger = admissions[1].game_changer_replay.input_bundle.portfolio_history.projection_binding
    assert first_ledger is not second_ledger                                                    # each point keeps its own independently verified ledger projection


def test_pointwise_policy_variation_is_never_compared_and_never_rejected() -> None:
    plan = plan_of(1, 2, 3)
    first = admission_at(plan.points[0], spec=MIXED_SPEC, auth=authority(zero=[I2]), trigger="0", destination="0")              # one sleeve/cash/authority/band policy ...
    second = admission_at(plan.points[1], spec=CASH_ONLY_SPEC, held=(), auth=authority(zero=[I1, I2, I3]), trigger="1", destination="0")   # ... a different one
    third = admission_at(plan.points[2], spec=MIXED_SPEC, auth=authority(zero=[I2]), trigger="0.2", destination="0.1")
    policies = [a.rebalance_replay.rebalance_plan.policy for a in (first, second, third)]
    assert policies[0] != policies[1] and policies[0] != policies[2]
    sleeves = [a.rebalance_replay.cross_universe_composition.candidate_eligibility.sleeves for a in (first, second, third)]
    assert sleeves[0] != sleeves[1]
    authorities = [a.rebalance_replay.cross_universe_composition.composition_plan.authority for a in (first, second, third)]
    assert authorities[0] != authorities[1]
    sequence = build(plan, H12, (first, second, third))                                        # varying policies are simply sequenced: no claim that one strategy was run through time
    assert len(sequence.admissions) == 3


def test_repeated_construction_is_equivalent() -> None:
    plan = plan_of(1, 2)
    admissions = tuple(admission_at(p) for p in plan.points)
    assert build(plan, H12, admissions) == build(plan, H12, admissions)


# --- R1: one owner, one portfolio, one bounded-context mode across the whole sequence --------------------------------------------------------

def history_of(admission):
    return admission.game_changer_replay.input_bundle.portfolio_history


def pf_of(owner=OWNER, pid=None, mode=PortfolioMode.MY_PORTFOLIO):
    return _make_portfolio(owner_id=owner, id=pid or SHARED_PF.id, mode=mode)


def point_and_horizon_rules_hold(plan, admissions, horizon=H12) -> None:
    """Every admission is individually complete and satisfies the pre-R1 D6 rules (positional point object identity, one horizon): only the cross-point portfolio rule can reject it."""
    assert len(admissions) == len(plan.points)
    for replay_point, admission in zip(plan.points, admissions):
        context = admission.game_changer_replay.input_bundle.analysis_context
        assert context.replay_point is replay_point and context.horizon is horizon


@pytest.mark.parametrize("mode", [PortfolioMode.MY_PORTFOLIO, PortfolioMode.SANDBOX])
def test_three_points_with_one_owner_one_portfolio_and_one_mode_succeed_while_everything_else_evolves(mode) -> None:
    plan = plan_of(1, 2, 3)
    pf = pf_of(mode=mode)
    admissions = tuple(admission_at(p, pf=pf, extra=index) for index, p in enumerate(plan.points))
    histories = [history_of(a) for a in admissions]
    assert {h.owner_id for h in histories} == {OWNER} and {h.portfolio_id for h in histories} == {pf.id}
    assert {h.projection_binding.projection.mode for h in histories} == {mode}
    assert len({id(h.projection_binding) for h in histories}) == 3                                   # different projection-binding objects across points
    assert len({id(h) for h in histories}) == 3                                                       # different coverage wrapper objects
    assert len({h.observed_at for h in histories}) == 3                                               # different observed_at values
    assert len({len(h.projection_binding.projection.known_transactions) for h in histories}) == 3   # the ledger content evolves between points
    point_and_horizon_rules_hold(plan, admissions)
    sequence = build(plan, H12, admissions)
    assert sequence.admissions is admissions


def test_the_default_shared_portfolio_fixture_is_my_portfolio_with_one_owner() -> None:
    plan = plan_of(1, 2)
    admissions = tuple(admission_at(p) for p in plan.points)
    assert {history_of(a).projection_binding.projection.mode for a in admissions} == {PortfolioMode.MY_PORTFOLIO}
    assert len({history_of(a).portfolio_id for a in admissions}) == 1 and len({history_of(a).owner_id for a in admissions}) == 1


@pytest.mark.parametrize("position", [1, 2])
def test_a_different_owner_at_a_later_point_is_rejected(position) -> None:
    plan = plan_of(1, 2, 3)
    admissions = [admission_at(p) for p in plan.points]
    admissions[position] = admission_at(plan.points[position], pf=pf_of(owner=OTHER_OWNER), owner=OTHER_OWNER)       # same portfolio UUID, different owner
    assert history_of(admissions[position]).portfolio_id == history_of(admissions[0]).portfolio_id and history_of(admissions[position]).owner_id != history_of(admissions[0]).owner_id
    point_and_horizon_rules_hold(plan, admissions)
    with pytest.raises(ValueError, match="owner"):
        build(plan, H12, tuple(admissions))


@pytest.mark.parametrize("position", [1, 2])
def test_a_different_portfolio_id_at_a_later_point_is_rejected_even_for_the_same_owner(position) -> None:
    plan = plan_of(1, 2, 3)
    admissions = [admission_at(p) for p in plan.points]
    other_portfolio = _make_portfolio(owner_id=OWNER)                                                    # same owner, another portfolio
    admissions[position] = admission_at(plan.points[position], pf=other_portfolio)
    assert history_of(admissions[position]).owner_id == history_of(admissions[0]).owner_id
    assert history_of(admissions[position]).portfolio_id != history_of(admissions[0]).portfolio_id
    point_and_horizon_rules_hold(plan, admissions)
    with pytest.raises(ValueError, match="portfolio id"):
        build(plan, H12, tuple(admissions))


def test_both_owner_and_portfolio_differing_and_portfolio_switching_are_rejected() -> None:
    plan = plan_of(1, 2)
    first = admission_at(plan.points[0])
    second = admission_at(plan.points[1], pf=_make_portfolio(owner_id=OTHER_OWNER), owner=OTHER_OWNER)
    point_and_horizon_rules_hold(plan, (first, second))
    with pytest.raises(ValueError):
        build(plan, H12, (first, second))
    with pytest.raises(ValueError):
        build(plan, H12, (admission_at(plan.points[0], pf=_make_portfolio(owner_id=OWNER)), admission_at(plan.points[1])))                  # switching portfolio at the second point


@pytest.mark.parametrize("first_mode,second_mode", [(PortfolioMode.MY_PORTFOLIO, PortfolioMode.SANDBOX), (PortfolioMode.SANDBOX, PortfolioMode.MY_PORTFOLIO)])
def test_crossing_my_portfolio_and_sandbox_inside_one_sequence_is_rejected_only_by_the_bounded_context_rule(first_mode, second_mode) -> None:
    plan = plan_of(1, 2)
    first = admission_at(plan.points[0], pf=pf_of(mode=first_mode))
    second = admission_at(plan.points[1], pf=pf_of(mode=second_mode))                                       # same owner and same portfolio UUID
    assert history_of(first).owner_id == history_of(second).owner_id and history_of(first).portfolio_id == history_of(second).portfolio_id
    assert history_of(first).projection_binding.projection.mode is first_mode and history_of(second).projection_binding.projection.mode is second_mode
    point_and_horizon_rules_hold(plan, (first, second))
    with pytest.raises(ValueError, match="PortfolioMode"):
        build(plan, H12, (first, second))
    with pytest.raises(ValueError, match="PortfolioMode"):
        PrivateBacktestDecisionReplaySequence(replay_plan=plan, horizon=H12, admissions=(first, second))


def test_direct_construction_enforces_the_same_cross_point_rules() -> None:
    plan = plan_of(1, 2)
    mixed_owner = (admission_at(plan.points[0]), admission_at(plan.points[1], pf=pf_of(owner=OTHER_OWNER), owner=OTHER_OWNER))
    mixed_portfolio = (admission_at(plan.points[0]), admission_at(plan.points[1], pf=_make_portfolio(owner_id=OWNER)))
    for bad in (mixed_owner, mixed_portfolio):
        with pytest.raises(ValueError):
            PrivateBacktestDecisionReplaySequence(replay_plan=plan, horizon=H12, admissions=bad)


def test_the_result_surface_and_stored_fields_are_unchanged_by_the_portfolio_rule() -> None:
    plan = plan_of(1, 2)
    _, sequence = sequence_for(plan)
    assert [f.name for f in dataclasses.fields(PrivateBacktestDecisionReplaySequence)] == ["replay_plan", "horizon", "admissions"]
    assert {n for n in dir(sequence) if not n.startswith("_")} == {"replay_plan", "horizon", "admissions"}
    for forbidden in ("owner_id", "portfolio_id", "portfolio_mode", "mode"):
        assert not hasattr(sequence, forbidden)


def test_different_valuation_currency_and_policies_across_points_remain_outside_the_portfolio_rule() -> None:
    plan = plan_of(1, 2)
    first = admission_at(plan.points[0], spec=MIXED_SPEC, auth=authority(zero=[I2]))
    second = admission_at(plan.points[1], spec=CASH_ONLY_SPEC, held=(), auth=authority(zero=[I1, I2, I3]), trigger="1")
    assert len(build(plan, H12, (first, second)).admissions) == 2


# --- direct construction ---------------------------------------------------------------------------------------------------------------------------

def test_direct_construction_reruns_the_same_validation() -> None:
    plan = plan_of(1, 2)
    admissions = tuple(admission_at(p) for p in plan.points)
    assert PrivateBacktestDecisionReplaySequence(replay_plan=plan, horizon=H12, admissions=admissions) == build(plan, H12, admissions)
    with pytest.raises(TypeError):
        PrivateBacktestDecisionReplaySequence(replay_plan=object(), horizon=H12, admissions=admissions)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        PrivateBacktestDecisionReplaySequence(replay_plan=plan, horizon="12M", admissions=admissions)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        PrivateBacktestDecisionReplaySequence(replay_plan=plan, horizon=H12, admissions=list(admissions))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PrivateBacktestDecisionReplaySequence(replay_plan=plan, horizon=H12, admissions=admissions[:1])
    with pytest.raises(ValueError):
        PrivateBacktestDecisionReplaySequence(replay_plan=plan, horizon=H12, admissions=tuple(reversed(admissions)))
    with pytest.raises(ValueError):
        PrivateBacktestDecisionReplaySequence(replay_plan=plan, horizon=H1, admissions=admissions)


# --- scope guards -----------------------------------------------------------------------------------------------------------------------------------

_PATH = Path(module_under_test.__file__)
_SOURCE = _PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/backtest_decision_replay_sequence.py"


def _names() -> set:
    return ({n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
            | {a.name for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) for a in n.names}
            | {n.name for n in ast.walk(_TREE) if isinstance(n, (ast.FunctionDef, ast.ClassDef))} | {a.arg for n in ast.walk(_TREE) if isinstance(n, ast.arguments)
                                                                                                       for a in (*n.args, *n.kwonlyargs)})


def test_no_decision_builder_recomputation_point_generation_or_policy_surface() -> None:
    assert not _names() & {"replay_private_backtest_game_changer_decision", "replay_private_backtest_candidate_eligibility", "build_private_backtest_marked_holdings_state",
                           "build_private_backtest_investable_cash_selection", "build_private_backtest_rebalance_current_state",
                           "build_private_backtest_cross_universe_composition", "replay_private_backtest_band_aware_rebalance",
                           "build_private_backtest_game_changer_plan_admission", "build_band_aware_rebalance_plan", "build_cash_first_rebalance_plan",
                           "build_cross_asset_composition_plan", "build_game_changer_decision_gate", "build_private_backtest_replay_plan", "build_private_backtest_replay_point",
                           "build_private_backtest_analysis_context", "PortfolioTransaction", "CashBucket", "PointInTimeMarketDataResolver", "scheduler", "dispatcher",
                           "repository", "supabase", "sorted", "reversed", "set", "frozenset", "max", "min", "sum"}
    assert not _names() & {"policy", "sleeves", "authority", "band", "friction", "cash_projection", "position_projection", "marked_positions", "post_trade_values",
                           "post_trade_cash", "trades", "blocked_buy_trades", "admission_state", "rebalance_plan", "knowledge_cutoff", "knowledge_cutoff_utc", "evaluation_date"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.BinOp, ast.AugAssign, ast.Break))]


def test_imports_are_only_the_replay_plan_the_d5b_admission_and_horizon() -> None:
    modules = {n.module for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("backend")}
    assert modules == {"backend.engine.private.backtest_replay_plan", "backend.engine.private.backtest_game_changer_plan_admission", "backend.engine.private.domain"}
    imported = {a.name for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("backend") for a in n.names}
    assert imported == {"PrivateBacktestReplayPlan", "PrivateBacktestGameChangerPlanAdmission", "Horizon"}


def test_no_performance_zero_event_sentinel_or_stop_semantics_in_any_identifier() -> None:
    identifiers = {n.lower() for n in _names()}
    for word in ("pnl", "profit", "loss", "sharpe", "sortino", "drawdown", "turnover", "performance", "benchmark", "alpha", "beta", "returns", "execut", "halt", "terminat", "stop"):
        assert not any(word in i for i in identifiers), word
    for word in ("nogamechangerevents", "emptygamechanger", "sentinel", "synthetic", "dummy", "fake"):
        assert not any(word in i for i in identifiers), word
    assert "return" not in identifiers


def test_no_clock_random_hash_io_or_generated_identity() -> None:
    assert not _names() & {"now", "utcnow", "today", "time", "uuid4", "uuid1", "random", "secrets", "urandom", "sha256", "hashlib", "hmac", "hash", "open",
                           "client", "rpc", "table", "environ", "getenv", "sleep", "datetime", "date", "json", "loads", "dumps", "float", "round", "quantize"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.Global, ast.Nonlocal, ast.AsyncFunctionDef, ast.Await))]


def test_module_is_outside_the_pure_manifest() -> None:
    assert _REL not in sg.PURE_MANIFEST


def test_documentation_and_ci_wiring() -> None:
    root = Path(__file__).resolve().parents[2]
    doc = (root / "docs" / "PRIVATE_BACKTEST_DECISION_REPLAY_SEQUENCE.md").read_text(encoding="utf-8")
    for needle in ("sequence/provenance closure", "exactly one", "object identity", "explicit Horizon", "no sorting", "no early stop", "no execution", "state propagation",
                   "no performance", "walk-forward", "policy-stability", "pointwise counterfactual", "macro", "zero-event", "no sentinel", "Phase 26 claim boundary",
                   "one owner and one portfolio", "owner_id and portfolio_id must remain equal", "PortfolioMode must remain one bounded context",
                   "projection bindings themselves are expected to differ", "coverage wrappers", "not proof of historical portfolio-metadata revision provenance",
                   "valuation currency is not made historically stable"):
        assert needle in doc, needle
    plan_doc = (root / "docs" / "PRIVATE_BACKTEST_REPLAY_PLAN.md").read_text(encoding="utf-8")
    assert "D6 is now the downstream consumer that binds exactly one completed D5B result to every replay-plan point in caller order" in plan_doc
    assert "Remote CI does not run the Phase 26" not in plan_doc and "permanently wired in CI" in plan_doc
    admission_doc = (root / "docs" / "PRIVATE_BACKTEST_GAME_CHANGER_PLAN_ADMISSION.md").read_text(encoding="utf-8")
    assert "D6 may sequence completed D5B admissions against a PrivateBacktestReplayPlan by exact replay-point identity and one explicit horizon" in admission_doc
    assert "D6 does not turn NOT_BLOCKED into approval and does not execute admitted plans" in admission_doc
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    architecture = ci.split("Phase 26 backtest architecture correctness", 1)[1].split("- name:", 1)[0]
    assert "backend/tests/test_backtest_decision_replay_sequence.py" in architecture
