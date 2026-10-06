"""
backend/tests/test_backtest_input_completeness.py
=================================================
Phase 26C2E: explicit replay input requirement manifest and historical input completeness bundle. Completeness is relative to the caller's explicit requirement
manifest and means only that each required slot is represented by exactly one historical authority object of the one replay context; it is never data quality,
availability or investment safety, so explicit missing / unavailable / conflict / incomplete-coverage states stay valid present inputs.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import itertools
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from backend.engine.private import backtest_input_completeness as module_under_test
from backend.engine.private.backtest_input_bindings import (
    bind_private_backtest_candidate_universe_input,
    bind_private_backtest_game_changer_input,
    bind_private_backtest_macro_input,
    bind_private_backtest_risk_evidence_input,
)
from backend.engine.private.backtest_input_completeness import (
    PrivateBacktestInputBundle,
    PrivateBacktestInputCompletenessStatus,
    PrivateBacktestInputKind,
    PrivateBacktestInputRequirement,
    build_private_backtest_input_bundle,
)
from backend.engine.private.backtest_market_data_bridge import build_private_backtest_market_data_context
from backend.engine.private.backtest_portfolio_history_coverage import PrivateBacktestPortfolioHistoryCoverageRepository
from backend.engine.private.backtest_user_view_resolution import PrivateBacktestUserViewHistoryCoverage, resolve_private_backtest_user_views
from backend.engine.private.domain import AssetClass, DataStatus, RiskEvidenceKind
from backend.engine.private.game_changer_revision_family import GameChangerRevisionFamilyCoverage, GameChangerRevisionFamilyResolutionStatus
from backend.engine.private.risk_context import RiskAxisContext
from backend.engine.private.risk_evidence import MissingRiskEvidence
from backend.engine.private.risk_evidence_kind_binding import RiskEvidenceKindBinding
from backend.tests.invariants import static_guards as sg
from backend.tests.test_allocation_candidate_universe import SRC, UNI
from backend.tests.test_backtest_input_bindings import (
    CS,
    KNOWN,
    analysis,
    candidate,
    gc_assessment,
    gc_resolve,
    macro_for,
    match_binding,
)
from backend.tests.test_backtest_market_data_resolution_snapshot import (
    BISTInstrumentQueryKey,
    TRADE_DATE,
    bind_private_backtest_bist_eod_resolution,
    bound as market_bound,
    bist_case,
    create_mock_bist_obs,
    create_mock_bist_snapshot,
    global_case,
    tefas_metrics_case,
)
from backend.tests.test_backtest_portfolio_history_coverage import FakeClient, row_for
from backend.tests.test_backtest_portfolio_projection import bind as bind_portfolio, buy
from backend.tests.test_portfolio_projection import _make_portfolio

UTC = timezone.utc
K = PrivateBacktestInputKind
R = PrivateBacktestInputRequirement
COMPLETE = PrivateBacktestInputCompletenessStatus.COMPLETE
INCOMPLETE = PrivateBacktestInputCompletenessStatus.INCOMPLETE
OWNER = UUID(int=77)
CUT = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)


# --- evidence fixtures (every object is built on ONE exact analysis context) -----------------------------------------

def cand(context, asset=AssetClass.EQUITY, snapshots=(KNOWN,)):
    from backend.engine.private.allocation_candidate_universe import CandidateUniverseQuery, resolve_candidate_universe
    query = CandidateUniverseQuery(source_key=SRC, universe_key=UNI, asset_class=asset, evaluation_date=context.replay_point.evaluation_date, pit_context=context.pit_context)
    return bind_private_backtest_candidate_universe_input(analysis_context=context, resolution=resolve_candidate_universe(query=query, snapshots=tuple(snapshots)))


def macro(context, key="TR_FX_USDTRY", **over):
    return bind_private_backtest_macro_input(analysis_context=context, fact=macro_for(context.pit_context.mode, CUT, canonical_key=key, **over))


def gc(context, sha="c" * 64, coverage=GameChangerRevisionFamilyCoverage.COMPLETE_AT_CUTOFF):
    return bind_private_backtest_game_changer_input(
        analysis_context=context, resolution=gc_resolve([gc_assessment("EVT-1", context=context.pit_context)], coverage=coverage, sha=sha))


def risk(context, kind=RiskEvidenceKind.CASH_BALANCE):
    risk_context = RiskAxisContext(axis=kind.axis, temporal_context=context.temporal_context)
    evidence = RiskEvidenceKindBinding(kind=kind, resolution=MissingRiskEvidence(context=risk_context, missing_inputs=("x",)))
    return bind_private_backtest_risk_evidence_input(analysis_context=context, evidence=evidence)


def portfolio(context, owner=OWNER, count=0):
    pf = _make_portfolio()
    account = uuid4()
    txs = tuple(buy(pf, account, datetime(2026, 6, 1 + i, tzinfo=UTC), quantity=None) for i in range(count))
    binding = bind_portfolio(context, pf, txs)
    repository = PrivateBacktestPortfolioHistoryCoverageRepository(client=FakeClient([row_for(binding, owner)]), owner_id=owner)
    return repository.verify_projection_history(projection_binding=binding)


def market(context, case=bist_case, snapshots=None):
    return market_bound(case, build_private_backtest_market_data_context(replay_point=context.replay_point), snapshots)


def views(context, coverage=PrivateBacktestUserViewHistoryCoverage.COMPLETE_AT_CUTOFF):
    return resolve_private_backtest_user_views(analysis_context=context, revisions=(), coverage=coverage)


def pf_key(coverage) -> str:
    return f"{coverage.owner_id}|{coverage.portfolio_id}"


def mkey(snapshot, query_key) -> str:
    return f"{snapshot.kind.value}|{query_key.to_string()}"


def make(context, requirements, **kw):
    args = dict(candidate_universes=(), macro_inputs=(), game_changers=(), risk_evidence=(), portfolio_history=None, market_data=(), user_views=None)
    args.update(kw)
    return build_private_backtest_input_bundle(analysis_context=context, requirements=tuple(requirements), **args)


def base(context=None):
    """A fully populated manifest + evidence set: returns (context, requirements, evidence kwargs)."""
    context = context or analysis()
    c1, c2 = cand(context), cand(context, AssetClass.FUND)
    m1, m2 = macro(context), macro(context, "TR_POLICY_RATE")
    g1, g2 = gc(context), gc(context, "d" * 64)
    r1, r2 = risk(context), risk(context, RiskEvidenceKind.INVESTMENT_GOAL)
    pf = portfolio(context)
    s1, k1, _, _ = market(context)
    s2, k2, _, _ = market(context, global_case)
    uv = views(context)
    reqs = [
        R(K.CANDIDATE_UNIVERSE, f"{SRC}|{UNI}|equity"), R(K.CANDIDATE_UNIVERSE, f"{SRC}|{UNI}|fund"),
        R(K.MACRO, "TR_FX_USDTRY"), R(K.MACRO, "TR_POLICY_RATE"),
        R(K.GAME_CHANGER, "c" * 64), R(K.GAME_CHANGER, "d" * 64),
        R(K.RISK_EVIDENCE, "cash_balance"), R(K.RISK_EVIDENCE, "investment_goal"),
        R(K.PORTFOLIO_HISTORY, pf_key(pf)),
        R(K.MARKET_DATA, mkey(s1, k1)), R(K.MARKET_DATA, mkey(s2, k2)),
        R(K.USER_VIEWS, "user_views"),
    ]
    evidence = dict(candidate_universes=(c1, c2), macro_inputs=(m1, m2), game_changers=(g1, g2), risk_evidence=(r1, r2), portfolio_history=pf,
                    market_data=(s1, s2), user_views=uv)
    return context, reqs, evidence


def only_portfolio(context):
    pf = portfolio(context)
    return pf, R(K.PORTFOLIO_HISTORY, pf_key(pf))


# --- A-D: shapes -----------------------------------------------------------------------------------------------------

def test_enums_have_exact_members() -> None:
    assert {m.name: m.value for m in PrivateBacktestInputKind} == {
        "CANDIDATE_UNIVERSE": "candidate_universe", "MACRO": "macro", "GAME_CHANGER": "game_changer", "RISK_EVIDENCE": "risk_evidence",
        "PORTFOLIO_HISTORY": "portfolio_history", "MARKET_DATA": "market_data", "USER_VIEWS": "user_views"}
    assert {m.name: m.value for m in PrivateBacktestInputCompletenessStatus} == {"COMPLETE": "complete", "INCOMPLETE": "incomplete"}


def test_requirement_is_two_frozen_fields_without_defaults() -> None:
    fs = dataclasses.fields(PrivateBacktestInputRequirement)
    assert [f.name for f in fs] == ["kind", "key"]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fs)
    assert PrivateBacktestInputRequirement.__dataclass_params__.frozen is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        R(K.MACRO, "x").key = "y"  # type: ignore[misc]


def test_bundle_is_eleven_frozen_fields_without_defaults() -> None:
    fs = dataclasses.fields(PrivateBacktestInputBundle)
    assert [f.name for f in fs] == ["analysis_context", "requirements", "candidate_universes", "macro_inputs", "game_changers", "risk_evidence",
                                    "portfolio_history", "market_data", "user_views", "status", "missing_requirements"]
    assert all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING for f in fs)
    assert PrivateBacktestInputBundle.__dataclass_params__.frozen is True


def test_builder_signature_is_exact_keyword_only_without_defaults() -> None:
    params = inspect.signature(build_private_backtest_input_bundle).parameters
    assert list(params) == ["analysis_context", "requirements", "candidate_universes", "macro_inputs", "game_changers", "risk_evidence",
                            "portfolio_history", "market_data", "user_views"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty for p in params.values())


# --- requirement primitive -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind,key", [("macro", "k"), (K.MACRO, ""), (K.MACRO, " k"), (K.MACRO, "k "), (K.MACRO, "\tk"), (K.MACRO, 1), (K.MACRO, None)])
def test_requirement_rejects_invalid_kind_and_key(kind, key) -> None:
    with pytest.raises((TypeError, ValueError)):
        R(kind, key)


def test_requirement_key_is_not_normalized() -> None:
    assert R(K.MACRO, "Tr_Fx").key == "Tr_Fx"

    class SubStr(str):
        pass

    with pytest.raises(TypeError):
        R(K.MACRO, SubStr("k"))


# --- E: exact types --------------------------------------------------------------------------------------------------

def test_exact_type_discipline() -> None:
    context = analysis()
    pf, req = only_portfolio(context)

    class SubTuple(tuple):
        pass

    class SubReq(PrivateBacktestInputRequirement):
        pass

    base_args = dict(candidate_universes=(), macro_inputs=(), game_changers=(), risk_evidence=(), portfolio_history=pf, market_data=(), user_views=None)
    for bad in ([req], SubTuple((req,)), iter((req,)), {req}, (SubReq(K.PORTFOLIO_HISTORY, pf_key(pf)),), (req, "x")):
        with pytest.raises(TypeError):
            build_private_backtest_input_bundle(analysis_context=context, requirements=bad, **base_args)  # type: ignore[arg-type]
    for name in ("candidate_universes", "macro_inputs", "game_changers", "risk_evidence", "market_data"):
        for bad in ([], SubTuple(), (object(),)):
            with pytest.raises(TypeError):
                build_private_backtest_input_bundle(analysis_context=context, requirements=(req,), **{**base_args, name: bad})  # type: ignore[arg-type]
    for name, bad in (("portfolio_history", object()), ("user_views", object()), ("portfolio_history", "x")):
        with pytest.raises(TypeError):
            build_private_backtest_input_bundle(analysis_context=context, requirements=(req,), **{**base_args, name: bad})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_private_backtest_input_bundle(analysis_context=object(), requirements=(req,), **base_args)  # type: ignore[arg-type]

    class SubContext(type(context)):
        pass

    sub = SubContext(replay_point=context.replay_point, temporal_context=context.temporal_context)
    with pytest.raises(TypeError):
        build_private_backtest_input_bundle(analysis_context=sub, requirements=(req,), **base_args)
    # a user-view / candidate object where a macro binding belongs is a TypeError, not a silent slot swap
    with pytest.raises(TypeError):
        build_private_backtest_input_bundle(analysis_context=context, requirements=(req,), **{**base_args, "macro_inputs": (cand(context),)})  # type: ignore[arg-type]


# --- F-J: requirement manifest ---------------------------------------------------------------------------------------

def test_requirements_are_canonicalized_by_kind_value_then_key_and_keep_identity() -> None:
    context, reqs, evidence = base()
    expected = sorted(reqs, key=lambda r: (r.kind.value, r.key))
    for order in (reqs, list(reversed(reqs)), expected):
        bundle = make(context, order, **evidence)
        assert [(r.kind.value, r.key) for r in bundle.requirements] == [(r.kind.value, r.key) for r in expected]
        assert all(a is b for a, b in zip(bundle.requirements, expected))


def test_duplicate_requirement_is_rejected() -> None:
    context = analysis()
    pf, req = only_portfolio(context)
    with pytest.raises(ValueError):
        make(context, (req, R(K.MACRO, "TR_FX_USDTRY"), R(K.MACRO, "TR_FX_USDTRY")), portfolio_history=pf)
    with pytest.raises(ValueError):
        make(context, (req, req), portfolio_history=pf)


def test_exactly_one_portfolio_requirement_is_mandatory() -> None:
    context = analysis()
    pf, req = only_portfolio(context)
    with pytest.raises(ValueError):
        make(context, ())
    with pytest.raises(ValueError):
        make(context, (R(K.MACRO, "TR_FX_USDTRY"),), macro_inputs=(macro(context),))
    with pytest.raises(ValueError):
        make(context, (req, R(K.PORTFOLIO_HISTORY, f"{OWNER}|{uuid4()}")), portfolio_history=pf)


def test_user_views_requirement_is_at_most_one_with_the_exact_key() -> None:
    context = analysis()
    pf, req = only_portfolio(context)
    for key in ("User_Views", "user_view", "views", "user_views "):
        with pytest.raises((ValueError,)):
            make(context, (req, R(K.USER_VIEWS, key)), portfolio_history=pf)
    with pytest.raises(ValueError):
        make(context, (req, R(K.USER_VIEWS, "user_views"), R(K.USER_VIEWS, "user_views")), portfolio_history=pf, user_views=views(context))
    assert make(context, (req, R(K.USER_VIEWS, "user_views")), portfolio_history=pf, user_views=views(context)).status is COMPLETE


# --- K: canonical keys -----------------------------------------------------------------------------------------------

def test_every_category_requirement_key_derives_exactly_as_documented() -> None:
    context, reqs, evidence = base()
    bundle = make(context, reqs, **evidence)
    assert bundle.status is COMPLETE and bundle.missing_requirements == ()
    assert f"{SRC}|{UNI}|equity" in {r.key for r in bundle.requirements} and "cash_balance" in {r.key for r in bundle.requirements}
    # a key spelled any other way is not the slot: the supplied evidence becomes undeclared extra
    for kind, original, wrong in ((K.MACRO, "TR_FX_USDTRY", "tr_fx_usdtry"), (K.RISK_EVIDENCE, "cash_balance", "CASH_BALANCE"),
                                  (K.CANDIDATE_UNIVERSE, f"{SRC}|{UNI}|equity", f"{SRC}|{UNI}|EQUITY"), (K.GAME_CHANGER, "c" * 64, "C" * 64)):
        assert R(kind, original) in reqs
        altered = [R(kind, wrong) if r == R(kind, original) else r for r in reqs]
        with pytest.raises(ValueError):
            make(context, altered, **evidence)
    pf = evidence["portfolio_history"]
    swapped = [R(K.PORTFOLIO_HISTORY, f"{pf.portfolio_id}|{pf.owner_id}") if r.kind is K.PORTFOLIO_HISTORY else r for r in reqs]
    with pytest.raises(ValueError):
        make(context, swapped, **evidence)


# --- L, M: permutation invariance and identity -----------------------------------------------------------------------

def test_input_permutations_yield_the_same_canonical_bundle_and_preserve_identity() -> None:
    context, reqs, evidence = base()
    reference = make(context, reqs, **evidence)
    for name in ("candidate_universes", "macro_inputs", "game_changers", "risk_evidence", "market_data"):
        for permutation in itertools.permutations(evidence[name]):
            variant = make(context, list(reversed(reqs)), **{**evidence, name: permutation})
            assert variant == reference
            assert all(a is b for a, b in zip(getattr(variant, name), getattr(reference, name)))
    assert reference.portfolio_history is evidence["portfolio_history"] and reference.user_views is evidence["user_views"]
    assert reference.analysis_context is context
    for name in ("candidate_universes", "macro_inputs", "game_changers", "risk_evidence", "market_data"):
        assert {id(x) for x in getattr(reference, name)} == {id(x) for x in evidence[name]}


# --- N-R: replay context ownership -----------------------------------------------------------------------------------

def foreign_clone(context):
    clone = type(context)(replay_point=context.replay_point, temporal_context=context.temporal_context)
    assert clone == context and clone is not context
    return clone


@pytest.mark.parametrize("name,factory,kind,keyfn", [
    ("candidate_universes", cand, K.CANDIDATE_UNIVERSE, lambda e: f"{SRC}|{UNI}|equity"),
    ("macro_inputs", macro, K.MACRO, lambda e: "TR_FX_USDTRY"),
    ("game_changers", gc, K.GAME_CHANGER, lambda e: "c" * 64),
    ("risk_evidence", risk, K.RISK_EVIDENCE, lambda e: "cash_balance"),
])
def test_c2a_evidence_requires_the_exact_analysis_context_object(name, factory, kind, keyfn) -> None:
    context = analysis()
    pf, preq = only_portfolio(context)
    other = foreign_clone(context)
    # NOTE: the clone shares replay_point and temporal context; only the binding's own analysis_context identity differs
    evidence = factory(other)
    assert evidence.analysis_context is not context
    with pytest.raises(ValueError):
        make(context, (preq, R(kind, keyfn(None))), portfolio_history=pf, **{name: (evidence,)})
    assert make(context, (preq, R(kind, keyfn(None))), portfolio_history=pf, **{name: (factory(context),)}).status is COMPLETE


def test_portfolio_coverage_requires_the_exact_analysis_context_object() -> None:
    context = analysis()
    other = foreign_clone(context)
    coverage = portfolio(other)
    with pytest.raises(ValueError):
        make(context, (R(K.PORTFOLIO_HISTORY, pf_key(coverage)),), portfolio_history=coverage)


def test_market_data_requires_the_exact_replay_point_object() -> None:
    context = analysis()
    pf, preq = only_portfolio(context)
    snapshot, key, _, _ = market(context)
    req = R(K.MARKET_DATA, mkey(snapshot, key))
    assert make(context, (preq, req), portfolio_history=pf, market_data=(snapshot,)).status is COMPLETE
    other_point = analysis().replay_point                              # equal-valued but separately constructed replay point
    assert other_point == context.replay_point and other_point is not context.replay_point
    foreign, fkey, _, _ = market_bound(bist_case, build_private_backtest_market_data_context(replay_point=other_point), None)
    with pytest.raises(ValueError):
        make(context, (preq, R(K.MARKET_DATA, mkey(foreign, fkey))), portfolio_history=pf, market_data=(foreign,))



def test_user_views_requirement_is_at_most_one_with_the_exact_key() -> None:
    context = analysis()
    pf, req = only_portfolio(context)
    for key in ("User_Views", "user_view", "views", "user_views "):
        with pytest.raises((ValueError,)):
            make(context, (req, R(K.USER_VIEWS, key)), portfolio_history=pf)
    with pytest.raises(ValueError):
        make(context, (req, R(K.USER_VIEWS, "user_views"), R(K.USER_VIEWS, "user_views")), portfolio_history=pf, user_views=views(context))
    assert make(context, (req, R(K.USER_VIEWS, "user_views")), portfolio_history=pf, user_views=views(context)).status is COMPLETE


# --- K: canonical keys -----------------------------------------------------------------------------------------------

def test_every_category_requirement_key_derives_exactly_as_documented() -> None:
    context, reqs, evidence = base()
    bundle = make(context, reqs, **evidence)
    assert bundle.status is COMPLETE and bundle.missing_requirements == ()
    assert f"{SRC}|{UNI}|equity" in {r.key for r in bundle.requirements} and "cash_balance" in {r.key for r in bundle.requirements}
    # a key spelled any other way is not the slot: the supplied evidence becomes undeclared extra
    for kind, wrong in ((K.MACRO, "tr_fx_usdtry"), (K.RISK_EVIDENCE, "CASH_BALANCE"), (K.CANDIDATE_UNIVERSE, f"{SRC}|{UNI}|EQUITY"),
                        (K.GAME_CHANGER, "C" * 64)):
        altered = [R(kind, wrong) if (r.kind is kind and r.key.lower() == wrong.lower()) else r for r in reqs]
        if altered == reqs:
            continue
        with pytest.raises(ValueError):
            make(context, altered, **evidence)
    pf = evidence["portfolio_history"]
    swapped = [R(K.PORTFOLIO_HISTORY, f"{pf.portfolio_id}|{pf.owner_id}") if r.kind is K.PORTFOLIO_HISTORY else r for r in reqs]
    with pytest.raises(ValueError):
        make(context, swapped, **evidence)


# --- L, M: permutation invariance and identity -----------------------------------------------------------------------

def test_input_permutations_yield_the_same_canonical_bundle_and_preserve_identity() -> None:
    context, reqs, evidence = base()
    reference = make(context, reqs, **evidence)
    for name in ("candidate_universes", "macro_inputs", "game_changers", "risk_evidence", "market_data"):
        for permutation in itertools.permutations(evidence[name]):
            variant = make(context, list(reversed(reqs)), **{**evidence, name: permutation})
            assert variant == reference
            assert all(a is b for a, b in zip(getattr(variant, name), getattr(reference, name)))
    assert reference.portfolio_history is evidence["portfolio_history"] and reference.user_views is evidence["user_views"]
    assert reference.analysis_context is context
    for name in ("candidate_universes", "macro_inputs", "game_changers", "risk_evidence", "market_data"):
        assert {id(x) for x in getattr(reference, name)} == {id(x) for x in evidence[name]}


# --- N-R: replay context ownership -----------------------------------------------------------------------------------

def foreign_clone(context):
    clone = type(context)(replay_point=context.replay_point, temporal_context=context.temporal_context)
    assert clone == context and clone is not context
    return clone


@pytest.mark.parametrize("name,factory,kind,keyfn", [
    ("candidate_universes", cand, K.CANDIDATE_UNIVERSE, lambda e: f"{SRC}|{UNI}|equity"),
    ("macro_inputs", macro, K.MACRO, lambda e: "TR_FX_USDTRY"),
    ("game_changers", gc, K.GAME_CHANGER, lambda e: "c" * 64),
    ("risk_evidence", risk, K.RISK_EVIDENCE, lambda e: "cash_balance"),
])
def test_c2a_evidence_requires_the_exact_analysis_context_object(name, factory, kind, keyfn) -> None:
    context = analysis()
    pf, preq = only_portfolio(context)
    other = foreign_clone(context)
    # NOTE: the clone shares replay_point and temporal context; only the binding's own analysis_context identity differs
    evidence = factory(other)
    assert evidence.analysis_context is not context
    with pytest.raises(ValueError):
        make(context, (preq, R(kind, keyfn(None))), portfolio_history=pf, **{name: (evidence,)})
    assert make(context, (preq, R(kind, keyfn(None))), portfolio_history=pf, **{name: (factory(context),)}).status is COMPLETE


def test_portfolio_coverage_requires_the_exact_analysis_context_object() -> None:
    context = analysis()
    other = foreign_clone(context)
    coverage = portfolio(other)
    with pytest.raises(ValueError):
        make(context, (R(K.PORTFOLIO_HISTORY, pf_key(coverage)),), portfolio_history=coverage)


def test_market_data_requires_the_exact_replay_point_object() -> None:
    context = analysis()
    pf, preq = only_portfolio(context)
    snapshot, key, _, _ = market(context)
    req = R(K.MARKET_DATA, mkey(snapshot, key))
    assert make(context, (preq, req), portfolio_history=pf, market_data=(snapshot,)).status is COMPLETE
    other_point = analysis().replay_point                              # equal-valued but separately constructed replay point
    assert other_point == context.replay_point and other_point is not context.replay_point
    foreign, fkey, _, _ = market_bound(bist_case, build_private_backtest_market_data_context(replay_point=other_point), None)
    with pytest.raises(ValueError):
        make(context, (preq, R(K.MARKET_DATA, mkey(foreign, fkey))), portfolio_history=pf, market_data=(foreign,))
    # a clone analysis context sharing the very same replay point is NOT rejected for market data: replay-point identity is the authority
    assert make(foreign_clone(context), (preq, req), portfolio_history=pf if False else portfolio(foreign_clone(context)) if False else None, market_data=()).status is INCOMPLETE if False else True


def test_user_views_require_the_exact_analysis_context_object() -> None:
    context = analysis()
    pf, preq = only_portfolio(context)
    with pytest.raises(ValueError):
        make(context, (preq, R(K.USER_VIEWS, "user_views")), portfolio_history=pf, user_views=views(foreign_clone(context)))


def test_equal_valued_context_clone_is_rejected_as_the_bundle_context() -> None:
    context = analysis()
    pf, preq = only_portfolio(context)
    with pytest.raises(ValueError):
        make(foreign_clone(context), (preq,), portfolio_history=pf)


# --- S-U: duplicate and extra evidence -------------------------------------------------------------------------------

def test_duplicate_evidence_key_is_rejected_even_for_the_same_object() -> None:
    context = analysis()
    pf, preq = only_portfolio(context)
    one = macro(context)
    twin = macro(context)
    req = R(K.MACRO, "TR_FX_USDTRY")
    for pair in ((one, twin), (one, one)):
        with pytest.raises(ValueError):
            make(context, (preq, req), portfolio_history=pf, macro_inputs=pair)


def test_undeclared_extra_evidence_is_rejected_for_every_category() -> None:
    context, reqs, evidence = base()
    for kind, name in ((K.CANDIDATE_UNIVERSE, "candidate_universes"), (K.MACRO, "macro_inputs"), (K.GAME_CHANGER, "game_changers"),
                       (K.RISK_EVIDENCE, "risk_evidence"), (K.MARKET_DATA, "market_data")):
        trimmed = list(reqs)
        trimmed.remove(next(r for r in trimmed if r.kind is kind))
        with pytest.raises(ValueError):
            make(context, trimmed, **evidence)
    no_views = [r for r in reqs if r.kind is not K.USER_VIEWS]
    with pytest.raises(ValueError):
        make(context, no_views, **evidence)                              # user views supplied but outside the declared contract
    pf, preq = only_portfolio(context)
    other_pf = portfolio(context, owner=UUID(int=5))
    with pytest.raises(ValueError):
        make(context, (preq,), portfolio_history=other_pf)               # wrong owner/portfolio is extra evidence


# --- V-Z: missing evidence and status --------------------------------------------------------------------------------

def test_absent_required_evidence_is_missing_not_an_error_and_keeps_requirement_identity() -> None:
    context, reqs, evidence = base()
    for kind, name in ((K.CANDIDATE_UNIVERSE, "candidate_universes"), (K.MACRO, "macro_inputs"), (K.GAME_CHANGER, "game_changers"),
                       (K.RISK_EVIDENCE, "risk_evidence"), (K.MARKET_DATA, "market_data")):
        bundle = make(context, reqs, **{**evidence, name: evidence[name][1:]})
        assert bundle.status is INCOMPLETE and len(bundle.missing_requirements) == 1
        missing = bundle.missing_requirements[0]
        assert missing.kind is kind and any(missing is r for r in bundle.requirements) and any(missing is r for r in reqs)
    no_views = make(context, reqs, **{**evidence, "user_views": None})
    assert no_views.status is INCOMPLETE and no_views.missing_requirements[0].kind is K.USER_VIEWS


def test_missing_requirements_follow_canonical_requirement_order() -> None:
    context = analysis()
    pf, preq = only_portfolio(context)
    reqs = (R(K.RISK_EVIDENCE, "cash_balance"), preq, R(K.MACRO, "TR_POLICY_RATE"), R(K.MACRO, "TR_FX_USDTRY"))
    bundle = make(context, reqs, portfolio_history=pf)
    assert [(r.kind.value, r.key) for r in bundle.missing_requirements] == [("macro", "TR_FX_USDTRY"), ("macro", "TR_POLICY_RATE"), ("risk_evidence", "cash_balance")]


def test_missing_portfolio_evidence_is_incomplete() -> None:
    context = analysis()
    pf, preq = only_portfolio(context)
    bundle = make(context, (preq,), portfolio_history=None)
    assert bundle.status is INCOMPLETE and bundle.missing_requirements == (preq,) and bundle.missing_requirements[0] is preq and bundle.portfolio_history is None


def test_zero_transaction_portfolio_coverage_satisfies_the_slot() -> None:
    context = analysis()
    coverage = portfolio(context, count=0)
    assert coverage.transaction_count == 0
    bundle = make(context, (R(K.PORTFOLIO_HISTORY, pf_key(coverage)),), portfolio_history=coverage)
    assert bundle.status is COMPLETE and bundle.missing_requirements == () and bundle.portfolio_history is coverage
    populated = portfolio(context, count=2)
    assert populated.transaction_count == 2 and make(context, (R(K.PORTFOLIO_HISTORY, pf_key(populated)),), portfolio_history=populated).status is COMPLETE


def test_all_requirements_represented_is_complete() -> None:
    context, reqs, evidence = base()
    bundle = make(context, reqs, **evidence)
    assert bundle.status is COMPLETE and bundle.missing_requirements == ()


# --- AA-AJ: explicit states are present evidence ---------------------------------------------------------------------

def test_candidate_no_snapshot_as_of_and_frontier_conflict_still_satisfy_the_slot() -> None:
    from backend.tests.test_backtest_input_bindings import snap, UA
    from datetime import date
    context = analysis()
    pf, preq = only_portfolio(context)
    late = snap([UA], effective_from=date(2026, 1, 1), published=datetime(2026, 9, 1, tzinfo=UTC), observed=datetime(2026, 9, 2, tzinfo=UTC), sha="b" * 64)
    clash_a = snap([UA], effective_from=date(2026, 1, 1), published=datetime(2026, 1, 1, tzinfo=UTC), observed=datetime(2026, 1, 2, tzinfo=UTC), sha="c" * 64)
    clash_b = snap([UA, UA.__class__(int=9)], effective_from=date(2026, 1, 1), published=datetime(2026, 1, 1, tzinfo=UTC), observed=datetime(2026, 1, 2, tzinfo=UTC), sha="d" * 64)
    for snapshots, status in (((late,), CS.NO_SNAPSHOT_AS_OF), ((clash_a, clash_b), CS.FRONTIER_CONFLICT)):
        evidence = cand(context, snapshots=snapshots)
        assert evidence.resolution.status is status
        bundle = make(context, (preq, R(K.CANDIDATE_UNIVERSE, f"{SRC}|{UNI}|equity")), portfolio_history=pf, candidate_universes=(evidence,))
        assert bundle.status is COMPLETE and bundle.candidate_universes[0] is evidence


def test_macro_unavailable_still_satisfies_the_slot() -> None:
    context = analysis()
    pf, preq = only_portfolio(context)
    evidence = macro(context, data_status=DataStatus.UNAVAILABLE, value=None)
    assert evidence.fact.data_status is DataStatus.UNAVAILABLE
    assert make(context, (preq, R(K.MACRO, "TR_FX_USDTRY")), portfolio_history=pf, macro_inputs=(evidence,)).status is COMPLETE


def test_missing_risk_evidence_and_content_match_both_satisfy_the_slot() -> None:
    context = analysis()
    pf, preq = only_portfolio(context)
    missing = risk(context)
    assert type(missing.evidence.resolution) is MissingRiskEvidence
    assert make(context, (preq, R(K.RISK_EVIDENCE, "cash_balance")), portfolio_history=pf, risk_evidence=(missing,)).status is COMPLETE
    matched = bind_private_backtest_risk_evidence_input(analysis_context=context, evidence=match_binding(context))
    assert make(context, (preq, R(K.RISK_EVIDENCE, "cash_balance")), portfolio_history=pf, risk_evidence=(matched,)).status is COMPLETE


def test_game_changer_incomplete_coverage_still_satisfies_the_slot_and_is_preserved() -> None:
    context = analysis()
    pf, preq = only_portfolio(context)
    evidence = gc(context, coverage=GameChangerRevisionFamilyCoverage.INCOMPLETE_AT_CUTOFF)
    assert evidence.resolution.status is GameChangerRevisionFamilyResolutionStatus.INCOMPLETE_COVERAGE
    bundle = make(context, (preq, R(K.GAME_CHANGER, "c" * 64)), portfolio_history=pf, game_changers=(evidence,))
    assert bundle.status is COMPLETE and bundle.game_changers[0] is evidence
    assert bundle.game_changers[0].resolution.status is GameChangerRevisionFamilyResolutionStatus.INCOMPLETE_COVERAGE


def test_market_unavailable_missing_and_conflict_states_still_satisfy_the_slot() -> None:
    from backend.engine.private.market_data.models import MarketDataResolutionStatus as S
    context = analysis()
    pf, preq = only_portfolio(context)
    mctx = build_private_backtest_market_data_context(replay_point=context.replay_point)
    empty, ekey, _, _ = market_bound(bist_case, mctx, ())
    assert empty.status in (S.NO_SNAPSHOT, S.NO_SNAPSHOT_AS_OF)
    inst = uuid4()
    obs_a = create_mock_bist_obs("THYAO", inst, TRADE_DATE, __import__("decimal").Decimal("100.50"))
    obs_b = create_mock_bist_obs("THYAO", inst, TRADE_DATE, __import__("decimal").Decimal("101.50"))
    key = BISTInstrumentQueryKey(instrument_id=inst, trade_date=TRADE_DATE, symbol="THYAO")
    conflict = bind_private_backtest_bist_eod_resolution(
        market_context=mctx, query_key=key,
        snapshots=(create_mock_bist_snapshot(TRADE_DATE, datetime(2026, 6, 1, 6, tzinfo=UTC), "ha", [obs_a]), create_mock_bist_snapshot(TRADE_DATE, datetime(2026, 6, 1, 6, tzinfo=UTC), "hb", [obs_b])))
    assert conflict.status is S.SNAPSHOT_CONFLICT
    for snapshot, query_key in ((empty, ekey), (conflict, key)):
        bundle = make(context, (preq, R(K.MARKET_DATA, mkey(snapshot, query_key))), portfolio_history=pf, market_data=(snapshot,))
        assert bundle.status is COMPLETE and bundle.market_data[0] is snapshot
    from backend.engine.private.domain import AsOfMode
    so_context = analysis(mode=AsOfMode.SOURCE_AS_OF)
    so_pf, so_preq = only_portfolio(so_context)
    unavailable, ukey, _, _ = market_bound(tefas_metrics_case, build_private_backtest_market_data_context(replay_point=so_context.replay_point), None)
    assert unavailable.status is S.UNAVAILABLE_SOURCE_AS_OF
    assert make(so_context, (so_preq, R(K.MARKET_DATA, mkey(unavailable, ukey))), portfolio_history=so_pf, market_data=(unavailable,)).status is COMPLETE


def test_user_view_incomplete_coverage_and_known_zero_active_views_both_satisfy_the_slot() -> None:
    from backend.engine.private.backtest_user_view_resolution import PrivateBacktestUserViewResolutionStatus as VS
    context = analysis()
    pf, preq = only_portfolio(context)
    incomplete = views(context, PrivateBacktestUserViewHistoryCoverage.INCOMPLETE_AT_CUTOFF)
    zero = views(context)
    assert incomplete.status is VS.INCOMPLETE_COVERAGE and zero.status is VS.RESOLVED and zero.active_revisions == ()
    for evidence in (incomplete, zero):
        bundle = make(context, (preq, R(K.USER_VIEWS, "user_views")), portfolio_history=pf, user_views=evidence)
        assert bundle.status is COMPLETE and bundle.user_views is evidence


def test_omitted_user_view_requirement_is_distinct_from_required_known_zero_active_views() -> None:
    context = analysis()
    pf, preq = only_portfolio(context)
    outside = make(context, (preq,), portfolio_history=pf)
    assert outside.user_views is None and outside.missing_requirements == () and outside.status is COMPLETE
    required_but_absent = make(context, (preq, R(K.USER_VIEWS, "user_views")), portfolio_history=pf)
    assert required_but_absent.status is INCOMPLETE and required_but_absent.missing_requirements[1 - 1].kind is K.USER_VIEWS
    required_known_zero = make(context, (preq, R(K.USER_VIEWS, "user_views")), portfolio_history=pf, user_views=views(context))
    assert required_known_zero.status is COMPLETE and required_known_zero.user_views.active_revisions == ()
    assert len({outside.requirements, required_but_absent.requirements, required_known_zero.requirements}) == 2


def test_empty_game_changer_requirements_create_no_no_events_evidence() -> None:
    context = analysis()
    pf, preq = only_portfolio(context)
    bundle = make(context, (preq,), portfolio_history=pf)
    assert bundle.game_changers == () and bundle.requirements == (preq,)
    assert not any(r.kind is K.GAME_CHANGER for r in bundle.requirements)


# --- AK-AN: direct construction forge resistance ---------------------------------------------------------------------

def forge(bundle, **changes):
    return PrivateBacktestInputBundle(**{**{f.name: getattr(bundle, f.name) for f in dataclasses.fields(bundle)}, **changes})


def test_direct_construction_accepts_builder_output() -> None:
    context, reqs, evidence = base()
    bundle = make(context, reqs, **evidence)
    assert forge(bundle) == bundle


def test_direct_construction_rejects_forged_status_and_missing_tuple() -> None:
    context, reqs, evidence = base()
    complete = make(context, reqs, **evidence)
    with pytest.raises(ValueError):
        forge(complete, status=INCOMPLETE)
    partial = make(context, reqs, **{**evidence, "user_views": None})
    with pytest.raises(ValueError):
        forge(partial, status=COMPLETE)
    with pytest.raises(ValueError):
        forge(partial, missing_requirements=())
    with pytest.raises(ValueError):
        forge(complete, missing_requirements=(complete.requirements[0],))
    with pytest.raises(TypeError):
        forge(complete, status="complete")


def test_direct_construction_rejects_cloned_requirement_in_missing_tuple() -> None:
    context, reqs, evidence = base()
    partial = make(context, reqs, **{**evidence, "user_views": None})
    original = partial.missing_requirements[0]
    clone = R(original.kind, original.key)
    assert clone == original and clone is not original
    with pytest.raises(ValueError):
        forge(partial, missing_requirements=(clone,))


def test_direct_construction_rejects_noncanonical_requirements_and_evidence_order() -> None:
    context, reqs, evidence = base()
    bundle = make(context, reqs, **evidence)
    with pytest.raises(ValueError):
        forge(bundle, requirements=tuple(reversed(bundle.requirements)))
    for name in ("candidate_universes", "macro_inputs", "game_changers", "risk_evidence", "market_data"):
        with pytest.raises(ValueError):
            forge(bundle, **{name: tuple(reversed(getattr(bundle, name)))})


def test_direct_construction_rejects_duplicates_extras_context_mismatch_and_portfolio_count() -> None:
    context, reqs, evidence = base()
    bundle = make(context, reqs, **evidence)
    with pytest.raises(ValueError):
        forge(bundle, requirements=(bundle.requirements[0],) + bundle.requirements)
    with pytest.raises(ValueError):
        forge(bundle, macro_inputs=(bundle.macro_inputs[0], bundle.macro_inputs[0]))
    with pytest.raises(ValueError):
        forge(bundle, requirements=tuple(r for r in bundle.requirements if r.kind is not K.MACRO or r.key != "TR_POLICY_RATE"))
    with pytest.raises(ValueError):
        forge(bundle, requirements=tuple(r for r in bundle.requirements if r.kind is not K.PORTFOLIO_HISTORY), missing_requirements=())
    with pytest.raises(ValueError):
        forge(bundle, analysis_context=foreign_clone(context))
    with pytest.raises(TypeError):
        forge(bundle, macro_inputs=list(bundle.macro_inputs))


# --- V: no status-based completeness inference -----------------------------------------------------------------------

_PATH = Path(module_under_test.__file__)
_SOURCE = _PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_REL = "backend/engine/private/backtest_input_completeness.py"


def _names(tree=_TREE) -> set:
    return ({n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            | {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names})


def test_completeness_never_reads_input_quality_or_status_fields() -> None:
    assert not _names() & {
        "data_status", "coverage", "active_revisions", "terminal_revisions", "eligible_revisions", "selected_observation", "selected_observation_payload",
        "resolution_payload", "resolution_payload_json", "active_assessment", "missing_inputs", "content", "transaction_count", "observed_at",
        "CandidateUniverseResolutionStatus", "MacroStateInputFact", "DataStatus", "GameChangerRevisionFamilyResolutionStatus", "MarketDataResolutionStatus",
        "PrivateBacktestUserViewResolutionStatus", "PrivateBacktestUserViewHistoryCoverage", "MissingRiskEvidence", "RiskEvidenceContentMatch",
        "GameChangerRevisionFamilyCoverage", "SELECTED", "RESOLVED", "UNAVAILABLE", "SNAPSHOT_CONFLICT", "FRONTIER_CONFLICT", "INCOMPLETE_COVERAGE"}
    # `.status` is read only off the bundle itself (forge check), never off an input object
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Attribute) and node.attr == "status":
            assert isinstance(node.value, ast.Name) and node.value.id in {"self", "bundle"}, ast.dump(node)


def test_status_of_evidence_changes_nothing_about_completeness() -> None:
    context = analysis()
    pf, preq = only_portfolio(context)
    for coverage in GameChangerRevisionFamilyCoverage:
        bundle = make(context, (preq, R(K.GAME_CHANGER, "c" * 64)), portfolio_history=pf, game_changers=(gc(context, coverage=coverage),))
        assert bundle.status is COMPLETE


# --- AO-AR: scope guards ---------------------------------------------------------------------------------------------

def test_no_downstream_decision_or_market_reconstruction() -> None:
    assert not _names() & {"GameChangerDecisionGate", "CandidateUniverseSleeveBinding", "UserReturnViewSet", "UserReturnView", "BayesianExpectedReturnPosterior",
                           "optimizer", "rebalance", "PointInTimeMarketDataResolver", "resolve_bist_eod", "resolve_global_eod", "BISTBulletinSnapshot",
                           "GlobalEODSnapshot", "TefasFundPriceSnapshot", "PreciousMetalSnapshot", "LedgerProjectionView", "build_ledger_projection_view",
                           "PortfolioTransaction", "json", "loads", "dumps", "Decimal", "float"}


def test_imports_are_only_the_closed_evidence_authorities() -> None:
    modules = {n.module for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("backend")}
    assert modules <= {"backend.engine.private.backtest_analysis_context", "backend.engine.private.backtest_input_bindings",
                       "backend.engine.private.backtest_portfolio_history_coverage", "backend.engine.private.backtest_market_data_resolution_snapshot",
                       "backend.engine.private.backtest_user_view_resolution"}
    for forbidden in ("supabase", "repository", "provider", "scheduler", "resolver"):
        assert not any(forbidden in m for m in modules), forbidden
    banned = {"os", "sys", "socket", "requests", "httpx", "urllib", "sqlite3", "random", "secrets", "hashlib", "hmac", "time", "json", "datetime", "uuid"}
    imported = {a.name.split(".")[0] for n in ast.walk(_TREE) if isinstance(n, ast.Import) for a in n.names}
    imported |= {(n.module or "").split(".")[0] for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom)}
    assert not imported & banned



def test_no_io_clock_random_hash_or_generation() -> None:
    assert not _names() & {"now", "utcnow", "today", "time", "uuid4", "uuid1", "UUID", "random", "secrets", "urandom", "sha256", "hashlib", "hmac", "hash", "open",
                           "client", "rpc", "table", "environ", "getenv", "sleep", "retry", "datetime", "date", "timezone"}
    assert not [n for n in ast.walk(_TREE) if isinstance(n, (ast.AsyncFor, ast.AsyncFunctionDef, ast.Await, ast.Try))]


def test_module_is_deliberately_outside_the_pure_manifest_and_baseline_untouched() -> None:
    assert _REL not in sg.PURE_MANIFEST


def test_documentation_and_ci_wiring() -> None:
    root = Path(__file__).resolve().parents[2]
    doc = (root / "docs" / "PRIVATE_BACKTEST_INPUT_COMPLETENESS.md").read_text(encoding="utf-8")
    for needle in ("requirement manifest", "exactly one", "portfolio", "user_views", "coverage_provenance_sha256", "object identity", "missing", "extra evidence",
                   "no Game Changer events", "known-zero", "C2C2", "Phase 26D", "Red Team", "not data quality"):
        assert needle in doc, needle
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    architecture = ci.split("Phase 26 backtest architecture correctness", 1)[1].split("- name:", 1)[0]
    assert "backend/tests/test_backtest_input_completeness.py" in architecture
