"""
backend/tests/test_tefas_fund_peer_percentile.py
================================================
Tests for the PIT-consistent same-category TEFAS peer cross-section and latest-window midrank percentile
(Phase 16H).

Only the LATEST Phase 16D rolling point of each peer is compared, inside one exact category label, one
horizon, one PIT context, and one evaluation calendar month. Coverage gaps are explicit; no historical
persistence, no annualization, no taxonomy normalization.
"""

from __future__ import annotations

import ast
import dataclasses
import decimal
import itertools
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from backend.engine.private import fund_peer_percentile as module_under_test
from backend.engine.private.domain import Currency, DataConfidenceLevel, Horizon, InstrumentType
from backend.engine.private.fund_category import (
    TefasFundCategoryLineage,
    TefasFundCategoryObservation,
    TefasFundCategoryUnavailable,
    TefasFundCategoryUnavailableReason,
    resolve_tefas_fund_category,
)
from backend.engine.private.fund_peer_percentile import (
    TefasFundPeerCandidate,
    TefasFundPeerCrossSection,
    TefasFundPeerMember,
    TefasFundPeerPercentile,
    build_tefas_fund_peer_cross_section,
    calculate_tefas_fund_peer_percentile,
)
from backend.engine.private.fund_price_series import (
    TefasFundPricePoint,
    TefasFundPriceSeries,
    build_tefas_fund_price_series,
)
from backend.engine.private.fund_rolling_returns import (
    TefasFundRollingReturnSeries,
    build_tefas_fund_rolling_return_series,
)
from backend.engine.private.market_data.models import (
    MarketDataResolutionMode,
    MarketDataResolutionStatus,
    TefasFundCurrentMetricsQueryKey,
)
from backend.engine.private.market_data.tefas_metrics_models import (
    TefasFundCurrentMetricsObservation,
    TefasFundMetricsSnapshot,
)
from backend.engine.private.market_data.tefas_models import (
    TefasFundPriceObservation,
    TefasFundPriceSnapshot,
    TefasObservationStatus,
)
from backend.engine.private.storage_models import compute_payload_hash

CR = MarketDataResolutionMode.CURRENT_REPORTED
SYS = MarketDataResolutionMode.SYSTEM_AS_OF
H12, H36 = Horizon.ALLOCATION_12M, Horizon.STRATEGIC_3Y
LABEL = "Hisse Senedi Fonu"
OTHER_LABEL = "Değişken Fon"
_T0 = datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
_CUTOFF = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
_SEP = (2026, 9)
_AUG = (2026, 8)

_UNIQUE_MSG = r"^TEFAS peer candidate instruments must be unique$"
_HORIZON_MSG = r"^TEFAS peer candidates must share the same rolling horizon$"
_PIT_MSG = r"^TEFAS peer candidates must share the same PIT context$"
_CAND_INSTRUMENT_MSG = r"^TEFAS peer candidate category and rolling series must reference the same instrument$"
_CAND_PIT_MSG = r"^TEFAS peer candidate category and rolling series must share the same PIT context$"
_MEMBER_AVAILABLE_MSG = r"^TEFAS peer member requires an available rolling return$"
_TARGET_CATEGORY_MSG = r"^target TEFAS fund category is unavailable$"
_TARGET_ROLLING_MSG = r"^target TEFAS fund rolling return is unavailable$"
_TARGET_PRESENT_MSG = r"^target TEFAS fund must occur exactly once among peer candidates$"
_MATCH_MSG = r"^peer cross-section must match the canonical derivation from its candidates exactly$"


def _iid(n: int) -> UUID:
    return UUID(int=n)


def _lineage(observed_at=_T0) -> TefasFundCategoryLineage:
    return TefasFundCategoryLineage(snapshot_id=uuid4(), snapshot_hash="a" * 64, observed_at=observed_at,
                                    metrics_resolution_key="b" * 64)


def _observation(instrument_id: UUID, label: str = LABEL, mode=CR, as_of=None) -> TefasFundCategoryObservation:
    return TefasFundCategoryObservation(instrument_id=instrument_id, category_label=label, mode=mode, as_of=as_of,
                                        lineage=_lineage())


def _unavailable(instrument_id: UUID, mode=CR, as_of=None,
                 reason=TefasFundCategoryUnavailableReason.CATEGORY_MISSING) -> TefasFundCategoryUnavailable:
    return TefasFundCategoryUnavailable(instrument_id=instrument_id, mode=mode, as_of=as_of, reason=reason,
                                        underlying_status=MarketDataResolutionStatus.SELECTED, lineage=None)


def _prices(instrument_id: UUID, prices, end_ym=_SEP, end_day: int = 28, mode=CR, as_of=None) -> TefasFundPriceSeries:
    n = len(prices)
    dated = []
    for i, price in enumerate(prices):
        index = end_ym[0] * 12 + (end_ym[1] - 1) - (n - 1 - i)
        year, month = index // 12, index % 12 + 1
        day = end_day if i == n - 1 else 15
        dated.append((date(year, month, day), price))
    points = tuple(TefasFundPricePoint(trade_date=d, unit_price=Decimal(p), currency=Currency.TRY,
                                       confidence=DataConfidenceLevel.HIGH, snapshot_retrieved_at=_T0,
                                       resolution_key=f"k-{instrument_id.int}-{d.isoformat()}") for d, p in dated)
    return TefasFundPriceSeries(instrument_id=instrument_id, mode=mode, as_of=as_of,
                                requested_dates=tuple(d for d, _ in dated), points=points, gaps=())


def _rolling(instrument_id: UUID, ret: str = "0.1", end_ym=_SEP, end_day: int = 28, mode=CR, as_of=None,
             horizon: Horizon = H12) -> TefasFundRollingReturnSeries:
    months = horizon.months
    prices = ["100"] * months + [str(Decimal("100") * (Decimal(1) + Decimal(ret)))]
    return build_tefas_fund_rolling_return_series(
        price_series=_prices(instrument_id, prices, end_ym, end_day, mode, as_of), horizon=horizon)


def _empty_rolling(instrument_id: UUID, mode=CR, as_of=None, horizon: Horizon = H12) -> TefasFundRollingReturnSeries:
    prices = [str(100 + i) for i in range(horizon.months)]
    rolling = build_tefas_fund_rolling_return_series(
        price_series=_prices(instrument_id, prices, mode=mode, as_of=as_of), horizon=horizon)
    assert rolling.points == ()
    return rolling


def _cand(n: int, ret: str = "0.1", label: str = LABEL, end_ym=_SEP, end_day: int = 28, mode=CR, as_of=None,
          horizon: Horizon = H12) -> TefasFundPeerCandidate:
    iid = _iid(n)
    return TefasFundPeerCandidate(category=_observation(iid, label, mode, as_of),
                                  rolling=_rolling(iid, ret, end_ym, end_day, mode, as_of, horizon))


def _section(target: int, cands) -> TefasFundPeerCrossSection:
    return build_tefas_fund_peer_cross_section(target_instrument_id=_iid(target), candidates=tuple(cands))


def _pct(section, n: int) -> Decimal:
    return calculate_tefas_fund_peer_percentile(cross_section=section, instrument_id=_iid(n)).percentile


class _Hostile:
    def __repr__(self) -> str:
        raise RuntimeError("hostile repr")


# --- candidate ----------------------------------------------------------------------------------

def test_candidate_fields_frozen_and_types() -> None:
    assert [f.name for f in dataclasses.fields(TefasFundPeerCandidate)] == ["category", "rolling"]
    c = _cand(1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.rolling = c.rolling  # type: ignore[misc]
    for bad in (None, "x", object(), _Hostile()):
        with pytest.raises(TypeError):
            TefasFundPeerCandidate(category=bad, rolling=c.rolling)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            TefasFundPeerCandidate(category=c.category, rolling=bad)  # type: ignore[arg-type]


def test_candidate_rejects_subclasses() -> None:
    class _Obs(TefasFundCategoryObservation):
        pass

    class _Roll(TefasFundRollingReturnSeries):
        pass
    c = _cand(1)
    sub_cat = _Obs(**{f.name: getattr(c.category, f.name) for f in dataclasses.fields(c.category)})
    with pytest.raises(TypeError):
        TefasFundPeerCandidate(category=sub_cat, rolling=c.rolling)
    sub_roll = _Roll(**{f.name: getattr(c.rolling, f.name) for f in dataclasses.fields(c.rolling)})
    with pytest.raises(TypeError):
        TefasFundPeerCandidate(category=c.category, rolling=sub_roll)


def test_candidate_instrument_binding_by_canonical_uuid() -> None:
    with pytest.raises(ValueError, match=_CAND_INSTRUMENT_MSG):
        TefasFundPeerCandidate(category=_observation(_iid(1)), rolling=_rolling(_iid(2)))
    with pytest.raises(ValueError, match=_CAND_INSTRUMENT_MSG):
        TefasFundPeerCandidate(category=_unavailable(_iid(1)), rolling=_rolling(_iid(2)))


def test_candidate_pit_binding() -> None:
    iid = _iid(1)
    with pytest.raises(ValueError, match=_CAND_PIT_MSG):  # category current, rolling historical
        TefasFundPeerCandidate(category=_observation(iid), rolling=_rolling(iid, mode=SYS, as_of=_CUTOFF))
    with pytest.raises(ValueError, match=_CAND_PIT_MSG):  # category historical, rolling current
        TefasFundPeerCandidate(category=_observation(iid, mode=SYS, as_of=_CUTOFF), rolling=_rolling(iid))
    with pytest.raises(ValueError, match=_CAND_PIT_MSG):  # T1 vs T2
        TefasFundPeerCandidate(category=_observation(iid, mode=SYS, as_of=_CUTOFF),
                               rolling=_rolling(iid, mode=SYS, as_of=_CUTOFF + timedelta(days=1)))
    assert TefasFundPeerCandidate(category=_observation(iid, mode=SYS, as_of=_CUTOFF),
                                  rolling=_rolling(iid, mode=SYS, as_of=_CUTOFF))


def test_candidate_may_have_empty_rolling_history_and_unavailable_category() -> None:
    iid = _iid(1)
    assert TefasFundPeerCandidate(category=_observation(iid), rolling=_empty_rolling(iid)).rolling.points == ()
    assert TefasFundPeerCandidate(category=_unavailable(iid), rolling=_rolling(iid))


# --- member ------------------------------------------------------------------------------------

def test_member_fields_properties_and_validation() -> None:
    assert [f.name for f in dataclasses.fields(TefasFundPeerMember)] == ["category", "rolling"]
    iid = _iid(3)
    member = TefasFundPeerMember(category=_observation(iid), rolling=_rolling(iid, "0.25", end_day=30))
    assert member.instrument_id == iid
    assert member.latest_return == Decimal("0.25")
    assert member.latest_end_date == date(2026, 9, 30)
    assert member.evaluation_month == (2026, 9)
    assert not any(f.name in ("instrument_id", "latest_return", "latest_end_date", "evaluation_month")
                   for f in dataclasses.fields(member))
    with pytest.raises(ValueError, match=_MEMBER_AVAILABLE_MSG):
        TefasFundPeerMember(category=_observation(iid), rolling=_empty_rolling(iid))
    with pytest.raises(TypeError):
        TefasFundPeerMember(category=_unavailable(iid), rolling=_rolling(iid))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=_CAND_INSTRUMENT_MSG):
        TefasFundPeerMember(category=_observation(_iid(1)), rolling=_rolling(_iid(2)))
    with pytest.raises(ValueError, match=_CAND_PIT_MSG):
        TefasFundPeerMember(category=_observation(iid), rolling=_rolling(iid, mode=SYS, as_of=_CUTOFF))


def test_member_uses_only_the_latest_rolling_point() -> None:
    iid = _iid(1)
    prices = ["100"] * 12 + ["110", "150"]  # two windows: 0.10 then 0.50
    rolling = build_tefas_fund_rolling_return_series(price_series=_prices(iid, prices), horizon=H12)
    assert [p.simple_return for p in rolling.points] == [Decimal("0.1"), Decimal("0.5")]
    member = TefasFundPeerMember(category=_observation(iid), rolling=rolling)
    assert member.latest_return == Decimal("0.5")


# --- cross-section: validation ----------------------------------------------------------------------

def test_cross_section_fields_frozen_and_derived_properties() -> None:
    assert [f.name for f in dataclasses.fields(TefasFundPeerCrossSection)] == [
        "target_instrument_id", "candidates", "members", "unclassified_instrument_ids",
        "unavailable_same_category_instrument_ids"]
    section = _section(2, [_cand(1, "0.1"), _cand(2, "0.2"), _cand(3, "0.3")])
    with pytest.raises(dataclasses.FrozenInstanceError):
        section.members = ()  # type: ignore[misc]
    assert section.category_label == LABEL and section.horizon is H12 and section.mode is CR
    assert section.as_of is None and section.evaluation_month == (2026, 9) and section.peer_count == 3
    assert section.is_complete is True
    for attr in ("all_tefas_funds", "universe", "rank", "score", "best", "worst"):
        assert not hasattr(section, attr)


@pytest.mark.parametrize("bad", [(), [], None, "x", (object(),), (None,)])
def test_candidates_must_be_a_non_empty_exact_tuple_of_candidates(bad) -> None:
    with pytest.raises((TypeError, ValueError)):
        build_tefas_fund_peer_cross_section(target_instrument_id=_iid(1), candidates=bad)  # type: ignore[arg-type]


def test_candidate_subclass_rejected() -> None:
    class _Sub(TefasFundPeerCandidate):
        pass
    c = _cand(1)
    with pytest.raises(TypeError):
        _section(1, [_Sub(category=c.category, rolling=c.rolling)])


def test_duplicate_instruments_rejected() -> None:
    with pytest.raises(ValueError, match=_UNIQUE_MSG):
        _section(1, [_cand(1), _cand(2), _cand(1, "0.5")])


def test_mixed_horizons_rejected() -> None:
    with pytest.raises(ValueError, match=_HORIZON_MSG):
        _section(1, [_cand(1), _cand(2, horizon=H36)])


def test_mixed_pit_contexts_rejected() -> None:
    with pytest.raises(ValueError, match=_PIT_MSG):
        _section(1, [_cand(1), _cand(2, mode=SYS, as_of=_CUTOFF)])
    with pytest.raises(ValueError, match=_PIT_MSG):
        _section(1, [_cand(1, mode=SYS, as_of=_CUTOFF), _cand(2, mode=SYS, as_of=_CUTOFF + timedelta(days=1))])


def test_target_must_be_present_with_category_and_rolling() -> None:
    with pytest.raises(ValueError, match=_TARGET_PRESENT_MSG):
        _section(9, [_cand(1), _cand(2)])
    unavailable_cat = TefasFundPeerCandidate(category=_unavailable(_iid(1)), rolling=_rolling(_iid(1)))
    with pytest.raises(ValueError, match=_TARGET_CATEGORY_MSG):
        _section(1, [unavailable_cat, _cand(2)])
    empty_roll = TefasFundPeerCandidate(category=_observation(_iid(1)), rolling=_empty_rolling(_iid(1)))
    with pytest.raises(ValueError, match=_TARGET_ROLLING_MSG):
        _section(1, [empty_roll, _cand(2)])


def test_target_argument_type_validation() -> None:
    for bad in (str(_iid(1)), None, 1, _Hostile()):
        with pytest.raises(TypeError):
            build_tefas_fund_peer_cross_section(target_instrument_id=bad, candidates=(_cand(1),))  # type: ignore[arg-type]


def test_builder_is_keyword_only() -> None:
    with pytest.raises(TypeError):
        build_tefas_fund_peer_cross_section(_iid(1), (_cand(1),))  # type: ignore[misc]


# --- membership / coverage ---------------------------------------------------------------------------------

def test_same_category_same_month_members_only() -> None:
    section = _section(1, [_cand(1, "0.1"), _cand(2, "0.2"), _cand(3, "0.3"),
                           _cand(4, "0.9", label=OTHER_LABEL)])
    assert [m.instrument_id for m in section.members] == [_iid(1), _iid(2), _iid(3)]
    assert section.unclassified_instrument_ids == () and section.unavailable_same_category_instrument_ids == ()
    assert section.is_complete and section.peer_count == 3


def test_different_category_creates_no_coverage_debt() -> None:
    section = _section(1, [_cand(1), _cand(2, label=OTHER_LABEL), _cand(3, label="hisse senedi fonu")])
    assert [m.instrument_id for m in section.members] == [_iid(1)]
    assert section.unclassified_instrument_ids == ()
    assert section.unavailable_same_category_instrument_ids == ()
    assert section.is_complete is True


def test_category_labels_are_never_normalized() -> None:
    section = _section(1, [_cand(1, label="Hisse Senedi Fonu"), _cand(2, label="hisse senedi fonu"),
                           _cand(3, label="Hisse  Senedi Fonu")])
    assert [m.instrument_id for m in section.members] == [_iid(1)]
    assert section.is_complete is True


def test_unclassified_candidates_are_reported_regardless_of_return() -> None:
    iid = _iid(4)
    unclassified = TefasFundPeerCandidate(category=_unavailable(iid), rolling=_rolling(iid, "0.99"))
    no_history = TefasFundPeerCandidate(category=_unavailable(_iid(5)), rolling=_empty_rolling(_iid(5)))
    section = _section(1, [_cand(1), _cand(2), unclassified, no_history])
    assert section.unclassified_instrument_ids == (_iid(4), _iid(5))
    assert [m.instrument_id for m in section.members] == [_iid(1), _iid(2)]
    assert section.is_complete is False and section.unavailable_same_category_instrument_ids == ()


def test_known_same_category_with_empty_rolling_is_unavailable_same_category() -> None:
    empty = TefasFundPeerCandidate(category=_observation(_iid(3)), rolling=_empty_rolling(_iid(3)))
    section = _section(1, [_cand(1), _cand(2), empty])
    assert section.unavailable_same_category_instrument_ids == (_iid(3),)
    assert [m.instrument_id for m in section.members] == [_iid(1), _iid(2)]
    assert section.is_complete is False


def test_different_category_with_empty_history_creates_no_same_category_debt() -> None:
    empty_other = TefasFundPeerCandidate(category=_observation(_iid(3), OTHER_LABEL),
                                         rolling=_empty_rolling(_iid(3)))
    section = _section(1, [_cand(1), empty_other])
    assert section.unavailable_same_category_instrument_ids == () and section.is_complete


def test_different_evaluation_month_is_not_compared_and_is_reported() -> None:
    stale = _cand(3, "0.9", end_ym=_AUG)
    section = _section(1, [_cand(1, "0.1"), _cand(2, "0.2"), stale])
    assert [m.instrument_id for m in section.members] == [_iid(1), _iid(2)]
    assert section.unavailable_same_category_instrument_ids == (_iid(3),)
    assert section.is_complete is False
    assert section.evaluation_month == (2026, 9)
    assert _pct(section, 2) == Decimal("75")  # the stale 0.9 return is never used


def test_same_month_different_day_of_month_is_included() -> None:
    section = _section(1, [_cand(1, "0.1", end_day=29), _cand(2, "0.2", end_day=30), _cand(3, "0.3", end_day=1)])
    assert section.peer_count == 3 and section.is_complete
    assert [m.latest_end_date.day for m in section.members] == [29, 30, 1]


def test_evaluation_month_comes_from_the_target() -> None:
    section = _section(1, [_cand(1, end_ym=_AUG), _cand(2, end_ym=_SEP)])
    assert section.evaluation_month == (2026, 8)
    assert [m.instrument_id for m in section.members] == [_iid(1)]
    assert section.unavailable_same_category_instrument_ids == (_iid(2),)


def test_partial_coverage_example() -> None:
    unclassified = TefasFundPeerCandidate(category=_unavailable(_iid(5)), rolling=_rolling(_iid(5), "0.7"))
    known_unaligned = _cand(4, "0.6", end_ym=_AUG)
    section = _section(1, [_cand(1, "0.1"), _cand(2, "0.2"), _cand(3, "0.3"), known_unaligned, unclassified,
                           _cand(6, "0.95", label=OTHER_LABEL)])
    assert [m.instrument_id for m in section.members] == [_iid(1), _iid(2), _iid(3)]
    assert len(section.unavailable_same_category_instrument_ids) == 1
    assert len(section.unclassified_instrument_ids) == 1
    assert section.is_complete is False
    result = calculate_tefas_fund_peer_percentile(cross_section=section, instrument_id=_iid(2))
    assert result.percentile == Decimal("50")
    assert result.is_complete is False and result.peer_count == 3


# --- ordering / determinism ---------------------------------------------------------------------------------

def test_candidate_order_does_not_change_output() -> None:
    cands = [_cand(5, "0.5"), _cand(1, "0.1"), _cand(3, "0.3"), _cand(2, "0.2", end_ym=_AUG),
             TefasFundPeerCandidate(category=_unavailable(_iid(4)), rolling=_rolling(_iid(4))),
             _cand(6, "0.6", label=OTHER_LABEL)]
    baseline = _section(1, cands)
    for perm in itertools.islice(itertools.permutations(cands), 60):
        section = _section(1, perm)
        assert section == baseline
        assert [c.category.instrument_id.int for c in section.candidates] == sorted(
            c.category.instrument_id.int for c in cands)
        assert _pct(section, 3) == _pct(baseline, 3)


def test_canonical_ordering_is_by_uuid_integer_ascending() -> None:
    hi, lo = _iid(2 ** 100), _iid(7)
    section = _section(7, [_cand(2 ** 100, "0.9"), _cand(7, "0.1")])
    assert [m.instrument_id for m in section.members] == [lo, hi]
    assert section.candidates[0].category.instrument_id == lo


# --- constructor forgery protection --------------------------------------------------------------------------

def _forge(section: TefasFundPeerCrossSection, **overrides) -> TefasFundPeerCrossSection:
    kwargs = dict(target_instrument_id=section.target_instrument_id, candidates=section.candidates,
                  members=section.members, unclassified_instrument_ids=section.unclassified_instrument_ids,
                  unavailable_same_category_instrument_ids=section.unavailable_same_category_instrument_ids)
    kwargs.update(overrides)
    return TefasFundPeerCrossSection(**kwargs)


def _rich_section():
    cands = [_cand(1, "0.1"), _cand(2, "0.2"), _cand(3, "0.3"), _cand(4, "0.4", end_ym=_AUG),
             TefasFundPeerCandidate(category=_unavailable(_iid(5)), rolling=_rolling(_iid(5))),
             _cand(6, "0.6", label=OTHER_LABEL)]
    return _section(1, cands)


def test_direct_construction_with_canonical_values_succeeds() -> None:
    section = _rich_section()
    assert _forge(section) == section


def test_forged_membership_and_exclusions_are_rejected() -> None:
    section = _rich_section()
    m1, m2, m3 = section.members
    other = TefasFundPeerMember(category=_observation(_iid(6), OTHER_LABEL), rolling=_rolling(_iid(6), "0.6"))
    stale = TefasFundPeerMember(category=_observation(_iid(4)), rolling=_rolling(_iid(4), "0.4", end_ym=_AUG))
    forgeries = [
        {"members": (m1, m2)},                       # omission
        {"members": (m1, m2, m3, other)},            # wrong-category peer
        {"members": (m1, m2, m3, stale)},            # wrong-month peer
        {"members": ()},
        {"members": (m2, m1, m3)},                   # wrong ordering
        {"members": (m1, m1, m2, m3)},               # duplicate
        {"unclassified_instrument_ids": ()},
        {"unclassified_instrument_ids": (_iid(5), _iid(6))},
        {"unavailable_same_category_instrument_ids": ()},
        {"unavailable_same_category_instrument_ids": (_iid(4), _iid(5))},
    ]
    for override in forgeries:
        with pytest.raises((ValueError, TypeError)):
            _forge(section, **override)


def test_forged_candidate_ordering_is_rejected() -> None:
    section = _rich_section()
    with pytest.raises(ValueError):
        _forge(section, candidates=tuple(reversed(section.candidates)))


def test_forged_target_and_candidates_are_rejected() -> None:
    section = _rich_section()
    with pytest.raises(ValueError):
        _forge(section, target_instrument_id=_iid(4))  # stale-month target re-derives a different member set
    with pytest.raises(ValueError):
        _forge(section, candidates=section.candidates[1:])


def test_forged_exclusion_tuples_must_be_exact_types() -> None:
    section = _rich_section()
    for bad in ([_iid(5)], (str(_iid(5)),), None, (_Hostile(),)):
        with pytest.raises((TypeError, ValueError)):
            _forge(section, unclassified_instrument_ids=bad)


# --- percentile: semantics ----------------------------------------------------------------------------------------

def _values(returns):
    section = _section(1, [_cand(i + 1, r) for i, r in enumerate(returns)])
    return [_pct(section, i + 1) for i in range(len(returns))]


def test_unique_four_peer_midrank_percentiles() -> None:
    assert _values(["0.10", "0.20", "0.30", "0.40"]) == [Decimal("12.5"), Decimal("37.5"), Decimal("62.5"),
                                                         Decimal("87.5")]


def test_tie_uses_midrank() -> None:
    assert _values(["0.10", "0.20", "0.20", "0.40"]) == [Decimal("12.5"), Decimal("50"), Decimal("50"),
                                                         Decimal("87.5")]


def test_all_equal_and_single_member() -> None:
    assert _values(["0.2", "0.2", "0.2"]) == [Decimal("50")] * 3
    assert _values(["0.2"]) == [Decimal("50")]


def test_ordering_compares_decimals_directly_across_extremes() -> None:
    assert _values(["-0.99", "0", "1E+30", "-0.5"]) == [Decimal("12.5"), Decimal("62.5"), Decimal("87.5"),
                                                     Decimal("37.5")]
    assert _values(["0.10", "0.1", "0.100"]) == [Decimal("50")] * 3  # numerically equal spellings tie


def test_repeating_percentile_is_deterministic_fifty_digit() -> None:
    values = _values(["0.1", "0.2", "0.3"])
    ctx = decimal.Context(prec=50, rounding=decimal.ROUND_HALF_EVEN, Emin=decimal.MIN_EMIN, Emax=decimal.MAX_EMAX)
    assert values[0] == ctx.divide(Decimal(50), Decimal(3))
    assert values[0] == Decimal("16.666666666666666666666666666666666666666666666667")
    assert Decimal(0) <= min(values) and max(values) <= Decimal(100)


def test_percentile_range_and_type() -> None:
    for returns in (["0.1"], ["0.1", "0.2"], ["0.1", "0.2", "0.3", "0.4", "0.5"]):
        for value in _values(returns):
            assert type(value) is Decimal and Decimal(0) <= value <= Decimal(100)


def test_percentile_result_fields_and_properties() -> None:
    assert [f.name for f in dataclasses.fields(TefasFundPeerPercentile)] == [
        "cross_section", "instrument_id", "percentile"]
    section = _section(1, [_cand(1, "0.1"), _cand(2, "0.2")])
    result = calculate_tefas_fund_peer_percentile(cross_section=section, instrument_id=_iid(2))
    assert result.cross_section is section and result.instrument_id == _iid(2)
    assert result.is_complete is True and result.peer_count == 2
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.percentile = Decimal(0)  # type: ignore[misc]
    for attr in ("rank", "label", "top_quartile", "score", "best", "winner", "grade", "signal"):
        assert not hasattr(result, attr)


def test_percentile_requires_a_member() -> None:
    other = _cand(3, label=OTHER_LABEL)
    section = _section(1, [_cand(1), _cand(2), other])
    for iid in (_iid(3), _iid(99)):
        with pytest.raises(ValueError):
            calculate_tefas_fund_peer_percentile(cross_section=section, instrument_id=iid)
    stale = _section(1, [_cand(1), _cand(2, end_ym=_AUG)])
    with pytest.raises(ValueError):
        calculate_tefas_fund_peer_percentile(cross_section=stale, instrument_id=_iid(2))


def test_percentile_argument_validation() -> None:
    section = _section(1, [_cand(1), _cand(2)])
    for bad in (None, "x", object(), _Hostile()):
        with pytest.raises(TypeError):
            calculate_tefas_fund_peer_percentile(cross_section=bad, instrument_id=_iid(1))  # type: ignore[arg-type]
    for bad in (str(_iid(1)), None, 1, _Hostile()):
        with pytest.raises(TypeError):
            calculate_tefas_fund_peer_percentile(cross_section=section, instrument_id=bad)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        calculate_tefas_fund_peer_percentile(section, _iid(1))  # type: ignore[misc]


def test_percentile_forgery_is_rejected() -> None:
    section = _section(1, [_cand(1, "0.1"), _cand(2, "0.2"), _cand(3, "0.3")])
    good = calculate_tefas_fund_peer_percentile(cross_section=section, instrument_id=_iid(2))
    assert TefasFundPeerPercentile(cross_section=section, instrument_id=_iid(2), percentile=good.percentile) == good
    for bad in (Decimal("51"), Decimal("0"), Decimal("100"), Decimal("50.0000000000000000000000000001")):
        with pytest.raises(ValueError):
            TefasFundPeerPercentile(cross_section=section, instrument_id=_iid(2), percentile=bad)
    for bad in (Decimal("-1"), Decimal("101"), Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(ValueError):
            TefasFundPeerPercentile(cross_section=section, instrument_id=_iid(2), percentile=bad)
    for bad in (50.0, 50, True, "50", None):
        with pytest.raises(TypeError):
            TefasFundPeerPercentile(cross_section=section, instrument_id=_iid(2), percentile=bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        TefasFundPeerPercentile(cross_section=section, instrument_id=_iid(99), percentile=Decimal("50"))


def test_percentile_independent_of_ambient_decimal_context() -> None:
    section = _section(1, [_cand(1, "0.1"), _cand(2, "0.2"), _cand(3, "0.3")])
    baseline = _pct(section, 1)
    for prec, rounding in ((4, decimal.ROUND_DOWN), (2, decimal.ROUND_UP), (9, decimal.ROUND_HALF_UP)):
        with decimal.localcontext() as ctx:
            ctx.prec = prec
            ctx.rounding = rounding
            assert _pct(section, 1) == baseline
            assert ctx.prec == prec and ctx.rounding == rounding
    before = (decimal.getcontext().prec, decimal.getcontext().rounding, dict(decimal.getcontext().flags))
    _pct(section, 1)
    assert before == (decimal.getcontext().prec, decimal.getcontext().rounding, dict(decimal.getcontext().flags))


def test_context_is_explicit_and_unrelated_errors_are_not_swallowed(monkeypatch) -> None:
    ctx = module_under_test._percentile_context()
    assert (ctx.prec, ctx.rounding, ctx.Emin, ctx.Emax) == (50, decimal.ROUND_HALF_EVEN, decimal.MIN_EMIN,
                                                            decimal.MAX_EMAX)
    section = _section(1, [_cand(1), _cand(2)])

    def boom():
        raise KeyError("unrelated")
    monkeypatch.setattr(module_under_test, "_percentile_context", boom)
    with pytest.raises(KeyError):
        calculate_tefas_fund_peer_percentile(cross_section=section, instrument_id=_iid(1))


def test_overflow_is_translated_to_a_static_range_error(monkeypatch) -> None:
    section = _section(1, [_cand(1), _cand(2)])
    tiny = decimal.Context(prec=50, Emin=-5, Emax=0, traps=[decimal.InvalidOperation, decimal.DivisionByZero,
                                                            decimal.Overflow])
    monkeypatch.setattr(module_under_test, "_percentile_context", lambda: tiny)
    with pytest.raises(ValueError, match=r"^TEFAS peer percentile exceeds supported Decimal analytics range$") as info:
        calculate_tefas_fund_peer_percentile(cross_section=section, instrument_id=_iid(1))
    assert info.value.__cause__ is None and info.value.__suppress_context__ is True


# --- CURRENT_REPORTED / SYSTEM_AS_OF -------------------------------------------------------------------------------

def test_current_reported_cross_section_preserves_pit_context() -> None:
    section = _section(2, [_cand(1, "0.1"), _cand(2, "0.2"), _cand(3, "0.3")])
    assert section.mode is CR and section.as_of is None
    for m in section.members:
        assert m.rolling.source.mode is CR and m.category.mode is CR
        assert m.rolling.source.as_of is None and m.category.as_of is None
    assert _pct(section, 2) == Decimal("50")


def test_system_as_of_cross_section_with_a_shared_cutoff() -> None:
    cands = [_cand(i, r, mode=SYS, as_of=_CUTOFF) for i, r in ((1, "0.1"), (2, "0.2"), (3, "0.3"))]
    section = _section(3, cands)
    assert section.mode is SYS and section.as_of == _CUTOFF
    assert [_pct(section, i) for i in (1, 2, 3)] == [Decimal("16.666666666666666666666666666666666666666666666667"),
                                                      Decimal("50"),
                                                      Decimal("83.333333333333333333333333333333333333333333333333")]


# --- end-to-end future isolation through the real upstream layers --------------------------------------------------

def _price_snap(instrument_id: UUID, retrieved_at: datetime, prices, end_ym=_SEP,
                payload_hash: str = "h") -> TefasFundPriceSnapshot:
    sid = uuid4()
    n = len(prices)
    obs = []
    for i, price in enumerate(prices):
        index = end_ym[0] * 12 + (end_ym[1] - 1) - (n - 1 - i)
        d = date(index // 12, index % 12 + 1, 28)
        obs.append(TefasFundPriceObservation(
            instrument_id=instrument_id, provider_symbol="X", trade_date=d, unit_price=Decimal(price),
            currency=Currency.TRY, instrument_type=InstrumentType.TEFAS_FUND, snapshot_id=sid,
            payload_hash=payload_hash, confidence_level=DataConfidenceLevel.HIGH))
    return TefasFundPriceSnapshot(
        id=sid, provider="TEFAS", provider_symbol="X", retrieved_at=retrieved_at, http_status=200,
        payload_hash=payload_hash, raw_payload="{}", instrument_id=instrument_id, period_months=60,
        trade_date_range=(obs[0].trade_date, obs[-1].trade_date), observations=obs)


def _category_payload(category) -> str:
    row = {"fonKodu": "X"}
    if category is not None:
        row["fonKategori"] = category
    return json.dumps({"resultList": [row]}, ensure_ascii=False)


def _metrics_snap(instrument_id: UUID, retrieved_at: datetime, payload: str) -> TefasFundMetricsSnapshot:
    sid = uuid4()
    p_hash = compute_payload_hash(payload)
    observation = TefasFundCurrentMetricsObservation(
        id=uuid4(), snapshot_id=sid, instrument_id=instrument_id, provider="TEFAS", provider_symbol="X",
        portfolio_size=Decimal("1000000"), portfolio_size_currency=Currency.TRY,
        outstanding_units=Decimal("1000"), investor_count=10, reported_current_unit_price=Decimal("100"),
        instrument_type=InstrumentType.TEFAS_FUND, payload_hash=p_hash, retrieved_at=retrieved_at,
        status=TefasObservationStatus.VALID, confidence_level=DataConfidenceLevel.MEDIUM)
    return TefasFundMetricsSnapshot(id=sid, provider="TEFAS", provider_symbol="X", retrieved_at=retrieved_at,
                                    http_status=200, payload_hash=p_hash, raw_payload=payload,
                                    instrument_id=instrument_id, endpoint="FUND_CURRENT_METRICS",
                                    observation=observation)


def _upstream_candidate(n: int, ret: str, price_snaps, metric_snaps, mode=SYS, as_of=_CUTOFF):
    iid = _iid(n)
    months = 12
    dates = tuple(date((_SEP[0] * 12 + _SEP[1] - 1 - (months - i)) // 12,
                       (_SEP[0] * 12 + _SEP[1] - 1 - (months - i)) % 12 + 1, 28) for i in range(months + 1))
    prices = build_tefas_fund_price_series(instrument_id=iid, trade_dates=dates, snapshots=price_snaps, mode=mode,
                                           as_of=as_of)
    rolling = build_tefas_fund_rolling_return_series(price_series=prices, horizon=H12)
    category = resolve_tefas_fund_category(
        query_key=TefasFundCurrentMetricsQueryKey(instrument_id=iid, provider_symbol="X"),
        snapshots=metric_snaps, mode=mode, as_of=as_of)
    return TefasFundPeerCandidate(category=category, rolling=rolling)


def test_future_upstream_information_cannot_alter_the_historical_cross_section() -> None:
    t_before = _CUTOFF - timedelta(days=5)
    t_after = _CUTOFF + timedelta(days=5)
    clean, polluted = [], []
    for n, ret in ((1, "0.1"), (2, "0.2"), (3, "0.3")):
        iid = _iid(n)
        prices = ["100"] * 12 + [str(Decimal("100") * (Decimal(1) + Decimal(ret)))]
        good_price = _price_snap(iid, t_before, prices)
        future_price = _price_snap(iid, t_after, ["100"] * 12 + ["999"], payload_hash="future")
        good_metrics = _metrics_snap(iid, t_before, _category_payload(LABEL))
        future_metrics = [_metrics_snap(iid, t_after, _category_payload(OTHER_LABEL)),
                          _metrics_snap(iid, t_after + timedelta(hours=1), "{")]
        clean.append(_upstream_candidate(n, ret, [good_price], [good_metrics]))
        polluted.append(_upstream_candidate(n, ret, [good_price, future_price], [good_metrics, *future_metrics]))
    assert all(type(c.category) is TefasFundCategoryObservation for c in polluted)
    s_clean = _section(2, clean)
    s_polluted = _section(2, polluted)
    assert [m.latest_return for m in s_polluted.members] == [m.latest_return for m in s_clean.members]
    assert [m.category.category_label for m in s_polluted.members] == [LABEL] * 3
    assert [_pct(s_polluted, i) for i in (1, 2, 3)] == [_pct(s_clean, i) for i in (1, 2, 3)]
    assert s_polluted.mode is SYS and s_polluted.as_of == _CUTOFF and s_polluted.is_complete


# --- no persistence / ranking -----------------------------------------------------------------------------------------------

def test_no_history_persistence_or_ranking_surface() -> None:
    for name in ("persistence", "percentile_series", "top_quartile", "median_percentile", "consecutive", "rank",
                 "score", "winner", "best", "worst", "normalize_category", "annualize", "fonKategori"):
        assert not hasattr(module_under_test, name)
    section = _section(1, [_cand(1), _cand(2)])
    assert not hasattr(section, "percentiles") and not hasattr(section, "history")


# --- purity / scope -----------------------------------------------------------------------------------------------------------

def _imports() -> set[str]:
    tree = ast.parse(Path(module_under_test.__file__).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    return found


def test_imports_are_limited_and_avoid_forbidden_dependencies() -> None:
    imports = _imports()
    private = {m for m in imports if m.startswith("backend.engine.private")}
    assert private == {"backend.engine.private.fund_category", "backend.engine.private.fund_rolling_returns"}
    for banned in ("pandas", "numpy", "scipy", "requests", "httpx", "os", "pathlib", "random", "secrets", "hashlib",
                   "hmac", "socket", "sqlite3", "math", "statistics", "json"):
        assert banned not in imports
    source = Path(module_under_test.__file__).read_text(encoding="utf-8")
    assert "float(" not in source


def test_module_is_clean_for_g1_g2_g4_g5_and_g3_only_flags_the_two_required_imports() -> None:
    from backend.tests.invariants import static_guards as sg

    rel = "backend/engine/private/fund_peer_percentile.py"
    source = (sg.REPO_ROOT / rel).read_text(encoding="utf-8")
    assert sg.scan_g1(source, rel) == []
    assert sg.scan_g2(source, rel) == []
    assert sg.scan_g4(source, rel) == []
    assert sg.scan_g5(source, rel) == []
    assert {v.node_kind for v in sg.scan_g3(source, rel)} == {
        "PrivateImport:backend.engine.private.fund_category",
        "PrivateImport:backend.engine.private.fund_rolling_returns",
    }
    assert rel not in sg.PURE_MANIFEST
