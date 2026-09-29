"""
backend/tests/test_tefas_fund_peer_persistence.py
=================================================
Tests for historical PIT peer-percentile persistence (Phase 16I).

persistence = count(percentile > 50) / valid historical percentile observations, stored as a fraction in [0, 1].
Each observation is an exact Phase 16H percentile built independently at its own SYSTEM_AS_OF cutoff.
"""

from __future__ import annotations

import ast
import dataclasses
import decimal
import itertools
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from backend.engine.private import fund_peer_persistence as module_under_test
from backend.engine.private.fund_category import TefasFundCategoryObservation, resolve_tefas_fund_category
from backend.engine.private.fund_peer_percentile import (
    TefasFundPeerCandidate,
    TefasFundPeerPercentile,
    build_tefas_fund_peer_cross_section,
    calculate_tefas_fund_peer_percentile,
)
from backend.engine.private.fund_peer_persistence import (
    TefasFundPeerPersistence,
    calculate_tefas_fund_peer_persistence,
)
from backend.engine.private.market_data.models import TefasFundCurrentMetricsQueryKey
from backend.tests.test_tefas_fund_peer_percentile import (
    CR,
    H12,
    H36,
    LABEL,
    OTHER_LABEL,
    SYS,
    _category_payload,
    _cand,
    _iid,
    _metrics_snap,
    _rolling,
    _unavailable,
)

T = _iid(1)
_SEP, _OCT, _NOV, _DEC = (2026, 9), (2026, 10), (2026, 11), (2026, 12)

_NO_OBS = r"^TEFAS peer persistence requires at least one historical percentile observation$"
_TARGET = r"^TEFAS peer persistence observations must target the same instrument$"
_HORIZON = r"^TEFAS peer persistence observations must share one rolling horizon$"
_SYS_ONLY = r"^TEFAS peer persistence requires SYSTEM_AS_OF historical observations$"
_DUP = r"^TEFAS peer persistence cannot contain duplicate evaluation months$"
_ASOF = r"^TEFAS peer persistence as_of values must increase with evaluation months$"
_ORDER = r"^TEFAS peer persistence observations must be in canonical evaluation-month order$"
_SCORE = r"^persistence_score must be finite and within \[0, 1\]$"
_MATCH = r"^persistence_score must match the canonical persistence ratio exactly$"


def _cutoff(ym) -> datetime:
    y, m = ym
    y, m = (y, m + 1) if m < 12 else (y + 1, 1)
    return datetime(y, m, 5, 12, 0, 0, tzinfo=timezone.utc)


def _pct(ym, kind="hi", *, as_of="auto", mode=SYS, horizon=H12, partial=False, target=1, label=LABEL,
         peers=None) -> TefasFundPeerPercentile:
    """Target percentile: hi = 83.33.., lo = 16.66.., mid = exactly 50 (sole member)."""
    cutoff = _cutoff(ym) if as_of == "auto" else as_of
    if mode is CR:
        cutoff = None
    tgt_ret, peer_rets = {"hi": ("0.9", ["0.1", "0.2"]), "lo": ("0.0", ["0.1", "0.2"]), "mid": ("0.5", [])}[kind]
    cands = [_cand(target, tgt_ret, label=label, end_ym=ym, mode=mode, as_of=cutoff, horizon=horizon)]
    for i, r in enumerate(peer_rets):
        cands.append(_cand(10 + i, r, label=label, end_ym=ym, mode=mode, as_of=cutoff, horizon=horizon))
    if partial:
        cands.append(TefasFundPeerCandidate(
            category=_unavailable(_iid(99), mode, cutoff),
            rolling=_rolling(_iid(99), "0.3", ym, mode=mode, as_of=cutoff, horizon=horizon)))
    section = build_tefas_fund_peer_cross_section(target_instrument_id=_iid(target), candidates=tuple(cands))
    return calculate_tefas_fund_peer_percentile(cross_section=section, instrument_id=_iid(target))


def _persist(percentiles, instrument_id=T):
    return calculate_tefas_fund_peer_persistence(instrument_id=instrument_id, percentiles=tuple(percentiles))


def _forge(p: TefasFundPeerPersistence, **over) -> TefasFundPeerPersistence:
    kw = dict(instrument_id=p.instrument_id, percentiles=p.percentiles, persistence_score=p.persistence_score)
    kw.update(over)
    return TefasFundPeerPersistence(**kw)


# --- shape ---------------------------------------------------------------------------------------------------

def test_fields_are_exactly_three_and_frozen() -> None:
    assert [f.name for f in dataclasses.fields(TefasFundPeerPersistence)] == [
        "instrument_id", "percentiles", "persistence_score"]
    p = _persist([_pct(_SEP)])
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.persistence_score = Decimal(0)  # type: ignore[misc]


def test_derived_properties_are_not_fields_and_no_forbidden_surface() -> None:
    p = _persist([_pct(_SEP), _pct(_OCT, "lo")])
    for name in ("horizon", "start_month", "end_month", "observation_count", "upper_half_count",
                 "partial_cross_section_count", "missing_month_count", "is_monthly_contiguous", "is_complete"):
        assert hasattr(p, name)
    for name in ("category_label", "dispersion", "std", "variance", "iqr", "label", "band", "rank", "score",
                 "recommendation", "category_change_score"):
        assert not hasattr(p, name)
        assert not hasattr(module_under_test, name)


# --- arithmetic --------------------------------------------------------------------------------------------------

def test_formula_examples() -> None:
    assert _persist([_pct(_SEP, "hi"), _pct(_OCT, "hi"), _pct(_NOV, "hi")]).persistence_score == Decimal(1)
    assert _persist([_pct(_SEP, "lo"), _pct(_OCT, "lo")]).persistence_score == Decimal(0)
    four = _persist([_pct(_SEP, "hi"), _pct(_OCT, "hi"), _pct(_NOV, "lo"), _pct(_DEC, "mid")])
    assert (four.upper_half_count, four.observation_count) == (2, 4)
    assert four.persistence_score == Decimal("0.5")
    assert type(four.persistence_score) is Decimal


def test_percentile_exactly_fifty_does_not_count() -> None:
    p = _persist([_pct(_SEP, "mid")])
    assert p.percentiles[0].percentile == Decimal(50)
    assert (p.upper_half_count, p.persistence_score) == (0, Decimal(0))
    three = _persist([_pct(_SEP, "hi"), _pct(_OCT, "lo"), _pct(_NOV, "mid")])
    assert three.upper_half_count == 1


def test_single_observation_is_defined_without_a_minimum_policy() -> None:
    assert _persist([_pct(_SEP, "hi")]).persistence_score == Decimal(1)
    assert _persist([_pct(_SEP, "lo")]).persistence_score == Decimal(0)
    assert _persist([_pct(_SEP, "hi")]).observation_count == 1


def test_repeating_score_is_exact_fifty_digit() -> None:
    p = _persist([_pct(_SEP, "hi"), _pct(_OCT, "hi"), _pct(_NOV, "lo")])
    ctx = decimal.Context(prec=50, rounding=decimal.ROUND_HALF_EVEN, Emin=decimal.MIN_EMIN, Emax=decimal.MAX_EMAX)
    assert p.persistence_score == ctx.divide(Decimal(2), Decimal(3))
    assert p.persistence_score == Decimal("0.66666666666666666666666666666666666666666666666667")


def test_exact_greater_than_fifty_comparison_without_rounding() -> None:
    base = [_pct(_SEP), _pct(_OCT), _pct(_NOV)]
    for p, value in zip(base, ("50", "50.000000000000000000000000000000000000000000000001",
                               "49.999999999999999999999999999999999999999999999999")):
        object.__setattr__(p, "percentile", Decimal(value))  # test-only: exact 16H values cannot be crafted
    result = _persist(base)
    assert result.upper_half_count == 1
    assert result.persistence_score == decimal.Context(prec=50).divide(Decimal(1), Decimal(3))


def test_ambient_decimal_context_is_ignored_and_untouched() -> None:
    items = [_pct(_SEP, "hi"), _pct(_OCT, "hi"), _pct(_NOV, "lo")]
    baseline = _persist(items)
    for prec, rounding in ((3, decimal.ROUND_DOWN), (2, decimal.ROUND_UP), (9, decimal.ROUND_HALF_UP)):
        with decimal.localcontext() as ctx:
            ctx.prec, ctx.rounding = prec, rounding
            assert _persist(items) == baseline
            assert (ctx.prec, ctx.rounding) == (prec, rounding)


def test_context_is_explicit() -> None:
    ctx = module_under_test._persistence_context()
    assert (ctx.prec, ctx.rounding, ctx.Emin, ctx.Emax) == (50, decimal.ROUND_HALF_EVEN, decimal.MIN_EMIN,
                                                            decimal.MAX_EMAX)


# --- input contract ----------------------------------------------------------------------------------------------------

def test_empty_history_rejected() -> None:
    with pytest.raises(ValueError, match=_NO_OBS):
        _persist([])


def test_argument_types() -> None:
    good = _pct(_SEP)
    for bad in (None, str(T), 1):
        with pytest.raises(TypeError):
            calculate_tefas_fund_peer_persistence(instrument_id=bad, percentiles=(good,))  # type: ignore[arg-type]
    for bad in ([good], None, "x", (good, object()), (0.5,)):
        with pytest.raises(TypeError):
            calculate_tefas_fund_peer_persistence(instrument_id=T, percentiles=bad)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        calculate_tefas_fund_peer_persistence(T, (good,))  # type: ignore[misc]


def test_percentile_subclass_rejected() -> None:
    class _Sub(TefasFundPeerPercentile):
        pass
    good = _pct(_SEP)
    sub = _Sub(cross_section=good.cross_section, instrument_id=good.instrument_id, percentile=good.percentile)
    with pytest.raises(TypeError):
        _persist([sub])


def test_target_binding() -> None:
    other = _pct(_SEP, target=2)
    with pytest.raises(ValueError, match=_TARGET):
        _persist([other])  # percentile.instrument_id != T
    section = _pct(_SEP).cross_section  # target T; percentile of a peer inside T's cross-section
    peer_member = calculate_tefas_fund_peer_percentile(cross_section=section, instrument_id=_iid(10))
    with pytest.raises(ValueError, match=_TARGET):
        _persist([peer_member], instrument_id=_iid(10))  # cross_section.target != instrument
    with pytest.raises(ValueError, match=_TARGET):
        _persist([_pct(_SEP), _pct(_OCT, target=2)])


def test_common_horizon() -> None:
    with pytest.raises(ValueError, match=_HORIZON):
        _persist([_pct(_SEP), _pct(_OCT, horizon=H36)])
    assert _persist([_pct(_SEP, horizon=H36), _pct(_OCT, horizon=H36)]).horizon is H36


def test_system_as_of_only() -> None:
    with pytest.raises(ValueError, match=_SYS_ONLY):
        _persist([_pct(_SEP, mode=CR)])
    with pytest.raises(ValueError, match=_SYS_ONLY):
        _persist([_pct(_SEP), _pct(_OCT, mode=CR)])


def test_duplicate_evaluation_month_rejected() -> None:
    a = _pct(_SEP, as_of=datetime(2026, 10, 5, tzinfo=timezone.utc))
    b = _pct(_SEP, "lo", as_of=datetime(2026, 10, 9, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match=_DUP):
        _persist([a, b])


def test_as_of_must_strictly_increase_with_months() -> None:
    later_month_earlier_cutoff = _pct(_OCT, as_of=datetime(2026, 10, 1, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match=_ASOF):
        _persist([_pct(_SEP), later_month_earlier_cutoff])
    with pytest.raises(ValueError, match=_ASOF):  # equal cutoffs are not strictly increasing
        _persist([_pct(_SEP, as_of=_cutoff(_OCT)), _pct(_OCT)])


# --- ordering / determinism -----------------------------------------------------------------------------------------------

def test_input_order_is_canonicalised() -> None:
    items = [_pct(_SEP, "hi"), _pct(_OCT, "lo"), _pct(_NOV, "hi"), _pct(_DEC, "mid")]
    baseline = _persist(items)
    assert [x.cross_section.evaluation_month for x in baseline.percentiles] == [_SEP, _OCT, _NOV, _DEC]
    for perm in itertools.permutations(items):
        got = _persist(perm)
        assert got == baseline and got.percentiles == baseline.percentiles
        assert got.persistence_score == baseline.persistence_score


def test_build_does_not_mutate_and_retains_observations_by_identity() -> None:
    items = (_pct(_OCT), _pct(_SEP))
    result = _persist(items)
    assert items[0].cross_section.evaluation_month == _OCT
    assert result.percentiles[0] is items[1] and result.percentiles[1] is items[0]


# --- coverage ---------------------------------------------------------------------------------------------------------------

def test_month_gap_is_reported_not_filled() -> None:
    p = _persist([_pct(_SEP, "hi"), _pct(_OCT, "lo"), _pct(_DEC, "hi")])
    assert p.observation_count == 3
    assert p.missing_month_count == 1
    assert p.is_monthly_contiguous is False and p.is_complete is False
    assert (p.start_month, p.end_month) == (_SEP, _DEC)
    assert p.persistence_score == Decimal("0.66666666666666666666666666666666666666666666666667")


def test_year_boundary_gap_counting() -> None:
    p = _persist([_pct((2026, 11), "hi"), _pct((2027, 2), "hi")])
    assert p.missing_month_count == 2 and p.persistence_score == Decimal(1)


def test_partial_cross_section_remains_in_denominator() -> None:
    p = _persist([_pct(_SEP, "hi"), _pct(_OCT, "hi", partial=True), _pct(_NOV, "lo")])
    assert p.observation_count == 3
    assert p.partial_cross_section_count == 1
    assert p.missing_month_count == 0 and p.is_monthly_contiguous is True
    assert p.is_complete is False
    assert p.upper_half_count == 2
    assert p.persistence_score == Decimal("0.66666666666666666666666666666666666666666666666667")


def test_complete_history() -> None:
    p = _persist([_pct(_SEP), _pct(_OCT, "lo"), _pct(_NOV)])
    assert p.missing_month_count == 0 and p.partial_cross_section_count == 0
    assert p.is_monthly_contiguous is True and p.is_complete is True
    assert all(x.is_complete for x in p.percentiles)


# --- category change / anti-lookahead --------------------------------------------------------------------------------------------

def test_category_may_change_and_is_not_rewritten() -> None:
    early = _pct(_SEP, "hi", label=LABEL)
    late = _pct(_OCT, "lo", label=OTHER_LABEL)
    result = _persist([late, early])
    assert result.percentiles[0] is early and result.percentiles[1] is late
    assert early.cross_section.category_label == LABEL
    assert late.cross_section.category_label == OTHER_LABEL
    assert [m.category.category_label for m in result.percentiles[0].cross_section.members] == [LABEL] * 3
    assert [m.category.category_label for m in result.percentiles[1].cross_section.members] == [OTHER_LABEL] * 3
    assert result.persistence_score == Decimal("0.5")
    assert not hasattr(result, "category_label")


def _real_percentile(ym, snaps):
    cutoff = _cutoff(ym)
    category = resolve_tefas_fund_category(
        query_key=TefasFundCurrentMetricsQueryKey(instrument_id=T, provider_symbol="X"),
        snapshots=snaps, mode=SYS, as_of=cutoff)
    assert type(category) is TefasFundCategoryObservation
    label = category.category_label
    target = TefasFundPeerCandidate(category=category, rolling=_rolling(T, "0.9", ym, mode=SYS, as_of=cutoff))
    peers = [_cand(10 + i, r, label=label, end_ym=ym, mode=SYS, as_of=cutoff) for i, r in enumerate(("0.1", "0.2"))]
    section = build_tefas_fund_peer_cross_section(target_instrument_id=T, candidates=(target, *peers))
    return calculate_tefas_fund_peer_percentile(cross_section=section, instrument_id=T)


def test_end_to_end_historical_category_and_future_isolation() -> None:
    t1 = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)       # before the September evaluation cutoff
    t2 = _cutoff(_SEP) + timedelta(days=10)                     # after September cutoff, before October's
    t3 = _cutoff(_OCT) + timedelta(days=10)                     # after every cutoff
    snap_a = _metrics_snap(T, t1, _category_payload(LABEL))
    snap_b = _metrics_snap(T, t2, _category_payload(OTHER_LABEL))
    snap_future = _metrics_snap(T, t3, _category_payload("Para Piyasası Fonu"))

    early_only = _real_percentile(_SEP, [snap_a])
    early_with_future = _real_percentile(_SEP, [snap_a, snap_b, snap_future])
    def _view(x):  # peer fixtures carry fresh uuid4 lineage ids, so compare the semantic content
        section = x.cross_section
        target_member = next(m for m in section.members if m.instrument_id == T)
        return (x.percentile, section.category_label, target_member.category.lineage,
                [m.latest_return for m in section.members], section.evaluation_month, section.as_of)

    assert _view(early_with_future) == _view(early_only)
    assert early_only.cross_section.category_label == LABEL
    assert _persist([early_only]).persistence_score == _persist([early_with_future]).persistence_score

    late = _real_percentile(_OCT, [snap_a, snap_b, snap_future])
    assert late.cross_section.category_label == OTHER_LABEL
    result = _persist([late, early_with_future])
    assert result.percentiles[0].cross_section.category_label == LABEL
    assert result.percentiles[1].cross_section.category_label == OTHER_LABEL
    assert result.percentiles[0] is early_with_future
    assert result.persistence_score == Decimal(1) and result.is_complete


# --- forgery ---------------------------------------------------------------------------------------------------------------------

def test_constructor_accepts_canonical_values_and_rejects_forgery() -> None:
    p = _persist([_pct(_SEP, "hi"), _pct(_OCT, "lo"), _pct(_NOV, "hi")])
    assert _forge(p) == p
    with pytest.raises(ValueError, match=_MATCH):
        _forge(p, persistence_score=Decimal("0.5"))
    with pytest.raises(ValueError, match=_MATCH):
        _forge(p, persistence_score=Decimal(1))
    with pytest.raises(ValueError, match=_TARGET):
        _forge(p, instrument_id=_iid(2))
    with pytest.raises(ValueError, match=_ORDER):
        _forge(p, percentiles=tuple(reversed(p.percentiles)))
    with pytest.raises(ValueError, match=_DUP):
        _forge(p, percentiles=(p.percentiles[0], p.percentiles[0], p.percentiles[2]))
    with pytest.raises(ValueError, match=_HORIZON):
        _forge(p, percentiles=(p.percentiles[0], _pct(_OCT, horizon=H36)))
    with pytest.raises(ValueError, match=_SYS_ONLY):
        _forge(p, percentiles=(p.percentiles[0], _pct(_OCT, mode=CR)))
    with pytest.raises(ValueError, match=_ASOF):
        _forge(p, percentiles=(p.percentiles[0], _pct(_OCT, as_of=datetime(2026, 10, 1, tzinfo=timezone.utc))))
    with pytest.raises(ValueError, match=_NO_OBS):
        _forge(p, percentiles=())


def test_score_type_and_range_validation() -> None:
    p = _persist([_pct(_SEP)])
    for bad in (Decimal("-0.1"), Decimal("1.1"), Decimal(100), Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(ValueError, match=_SCORE):
            _forge(p, persistence_score=bad)
    for bad in (1.0, 1, True, "1", None):
        with pytest.raises(TypeError):
            _forge(p, persistence_score=bad)
    for bad in ([p.percentiles[0]], None, (object(),)):
        with pytest.raises(TypeError):
            _forge(p, percentiles=bad)
    with pytest.raises(TypeError):
        _forge(p, instrument_id=str(T))


# --- purity / scope ------------------------------------------------------------------------------------------------------------------

def _imports() -> set[str]:
    tree = ast.parse(Path(module_under_test.__file__).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    return found


def test_imports_are_limited() -> None:
    imports = _imports()
    assert {m for m in imports if m.startswith("backend.")} == {"backend.engine.private.fund_peer_percentile"}
    assert imports <= {"__future__", "dataclasses", "decimal", "uuid", "backend.engine.private.fund_peer_percentile"}
    assert "float(" not in Path(module_under_test.__file__).read_text(encoding="utf-8")


def test_module_is_clean_for_g1_g2_g4_g5_and_g3_only_flags_the_required_import() -> None:
    from backend.tests.invariants import static_guards as sg

    rel = "backend/engine/private/fund_peer_persistence.py"
    source = (sg.REPO_ROOT / rel).read_text(encoding="utf-8")
    assert sg.scan_g1(source, rel) == []
    assert sg.scan_g2(source, rel) == []
    assert sg.scan_g4(source, rel) == []
    assert sg.scan_g5(source, rel) == []
    assert {v.node_kind for v in sg.scan_g3(source, rel)} == {
        "PrivateImport:backend.engine.private.fund_peer_percentile"}
    assert rel not in sg.PURE_MANIFEST
