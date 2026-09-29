"""
backend/engine/private/fund_peer_percentile.py
==============================================
PIT-consistent same-category TEFAS peer cross-section and latest rolling-return percentile for the Private
Investment Decision Engine (Phase 16H).

Architectural Invariants:
    - Pure domain module. Zero network, filesystem, database, ambient clock, UUID generation, randomness, hashing,
      float arithmetic, pandas/numpy/scipy, persistence, or provider/resolver calls. It consumes already-built
      Phase 16G category resolutions and Phase 16D rolling-return series; it never resolves or reads upstream data.
    - A candidate binds one category resolution to one rolling series: same instrument, same PIT mode, same as_of.
      All candidates in a cross-section share one rolling horizon and one PIT context (`mode` by identity, `as_of`
      by equality). No cross-PIT or cross-horizon comparison is possible.
    - Peers are compared ONLY on the latest rolling point (the latest month-end window). No historical percentile
      series, no persistence, no annualization: within one horizon annualization is monotonic, so ordering the
      cumulative simple returns is equivalent and needs no dependency on the annualized layer.
    - The target defines the peer group: its exact provider category label (no case/whitespace/taxonomy
      normalization) and its latest evaluation calendar month (year, month; day of month need not match). A
      candidate is a MEMBER only if its category is an observation with the identical label, its rolling series is
      non-empty and its latest window ends in the target's month.
    - Coverage is explicit, never silently dropped: candidates whose category is unavailable are reported in
      `unclassified_instrument_ids`; candidates known to share the target's label but lacking a comparable latest
      window (empty rolling history or a different latest month) are reported in
      `unavailable_same_category_instrument_ids`. Candidates of a different known label create no debt.
      `is_complete` is True only when both tuples are empty. A partial cross-section still yields a percentile,
      carrying `is_complete == False`; missing is never treated as zero and never fabricated.
    - Canonical order is `instrument_id.int` ascending for candidates, members and both id tuples. The builder
      accepts any candidate order; the constructor requires canonical order and recomputes the members and both
      exclusion tuples through the same private helper as the builder, rejecting forgery.
    - Percentile is the midrank percentile `100 * (less + equal / 2) / N` over the members' latest simple returns,
      evaluated exactly as `50 * (2 * less + equal) / N` in an explicit fresh 50-digit ROUND_HALF_EVEN context with
      maximum exponent range (never the ambient context). Result lies in [0, 100] and is a percent, not a fraction.
      Only `decimal.Overflow` is translated; other errors propagate.
    - No ranking label, score, top-quartile flag, best/worst, minimum peer-count policy, candidate discovery,
      category taxonomy normalization, or historical category effective dating.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from backend.engine.private.fund_category import (
    TefasFundCategoryObservation,
    TefasFundCategoryResolution,
    TefasFundCategoryUnavailable,
)
from backend.engine.private.fund_rolling_returns import TefasFundRollingReturnSeries

_ERR_CAND_CATEGORY_TYPE = "category must be an exact TefasFundCategoryObservation or TefasFundCategoryUnavailable"
_ERR_ROLLING_TYPE = "rolling must be an exact TefasFundRollingReturnSeries instance"
_ERR_CAND_INSTRUMENT = "TEFAS peer candidate category and rolling series must reference the same instrument"
_ERR_CAND_PIT = "TEFAS peer candidate category and rolling series must share the same PIT context"
_ERR_MEMBER_CATEGORY_TYPE = "category must be an exact TefasFundCategoryObservation instance"
_ERR_MEMBER_AVAILABLE = "TEFAS peer member requires an available rolling return"
_ERR_TARGET_TYPE = "target_instrument_id must be an exact UUID instance"
_ERR_CANDIDATES_TYPE = "candidates must be a non-empty tuple of exact TefasFundPeerCandidate instances"
_ERR_UNIQUE = "TEFAS peer candidate instruments must be unique"
_ERR_HORIZON = "TEFAS peer candidates must share the same rolling horizon"
_ERR_PIT = "TEFAS peer candidates must share the same PIT context"
_ERR_TARGET_PRESENT = "target TEFAS fund must occur exactly once among peer candidates"
_ERR_TARGET_CATEGORY = "target TEFAS fund category is unavailable"
_ERR_TARGET_ROLLING = "target TEFAS fund rolling return is unavailable"
_ERR_ORDER = "peer candidates must be in canonical instrument_id order"
_ERR_MEMBERS_TYPE = "members must be a tuple of exact TefasFundPeerMember instances"
_ERR_IDS_TYPE = "instrument id collections must be tuples of exact UUID instances"
_ERR_MATCH = "peer cross-section must match the canonical derivation from its candidates exactly"
_ERR_CROSS_SECTION_TYPE = "cross_section must be an exact TefasFundPeerCrossSection instance"
_ERR_INSTRUMENT_TYPE = "instrument_id must be an exact UUID instance"
_ERR_NOT_MEMBER = "instrument must be a member of the peer cross-section"
_ERR_PERCENTILE_TYPE = "percentile must be an exact Decimal instance"
_ERR_PERCENTILE_RANGE = "percentile must be finite and within [0, 100]"
_ERR_PERCENTILE_MATCH = "percentile must match the canonical midrank percentile exactly"
_ERR_RANGE = "TEFAS peer percentile exceeds supported Decimal analytics range"


def _percentile_context() -> decimal.Context:
    return decimal.Context(
        prec=50,
        rounding=decimal.ROUND_HALF_EVEN,
        Emin=decimal.MIN_EMIN,
        Emax=decimal.MAX_EMAX,
    )


def _require_category_rolling_binding(category: object, rolling: TefasFundRollingReturnSeries) -> None:
    if category.instrument_id != rolling.source.instrument_id:  # type: ignore[attr-defined]
        raise ValueError(_ERR_CAND_INSTRUMENT)
    if category.mode is not rolling.source.mode or category.as_of != rolling.source.as_of:  # type: ignore[attr-defined]
        raise ValueError(_ERR_CAND_PIT)


@dataclass(frozen=True)
class TefasFundPeerCandidate:
    """One fund's category resolution bound to its rolling-return series (same instrument and PIT context)."""
    category: TefasFundCategoryResolution
    rolling: TefasFundRollingReturnSeries

    def __post_init__(self) -> None:
        if type(self.category) not in (TefasFundCategoryObservation, TefasFundCategoryUnavailable):
            raise TypeError(_ERR_CAND_CATEGORY_TYPE)
        if type(self.rolling) is not TefasFundRollingReturnSeries:
            raise TypeError(_ERR_ROLLING_TYPE)
        _require_category_rolling_binding(self.category, self.rolling)


@dataclass(frozen=True)
class TefasFundPeerMember:
    """A classified fund with an available latest rolling return."""
    category: TefasFundCategoryObservation
    rolling: TefasFundRollingReturnSeries

    def __post_init__(self) -> None:
        if type(self.category) is not TefasFundCategoryObservation:
            raise TypeError(_ERR_MEMBER_CATEGORY_TYPE)
        if type(self.rolling) is not TefasFundRollingReturnSeries:
            raise TypeError(_ERR_ROLLING_TYPE)
        _require_category_rolling_binding(self.category, self.rolling)
        if not self.rolling.points:
            raise ValueError(_ERR_MEMBER_AVAILABLE)

    @property
    def instrument_id(self) -> UUID:
        return self.category.instrument_id

    @property
    def latest_return(self) -> Decimal:
        return self.rolling.points[-1].simple_return

    @property
    def latest_end_date(self) -> date:
        return self.rolling.points[-1].end_date

    @property
    def evaluation_month(self) -> tuple[int, int]:
        end = self.latest_end_date
        return (end.year, end.month)


def _candidate_id(candidate: TefasFundPeerCandidate) -> UUID:
    return candidate.category.instrument_id


def _require_candidates(candidates: object) -> tuple[TefasFundPeerCandidate, ...]:
    if (
        type(candidates) is not tuple
        or len(candidates) == 0
        or any(type(c) is not TefasFundPeerCandidate for c in candidates)
    ):
        raise TypeError(_ERR_CANDIDATES_TYPE)
    return candidates


def _require_consistent(target: UUID, candidates: tuple[TefasFundPeerCandidate, ...]) -> TefasFundPeerCandidate:
    ids = [_candidate_id(c) for c in candidates]
    if len(set(ids)) != len(ids):
        raise ValueError(_ERR_UNIQUE)
    first = candidates[0]
    if any(c.rolling.horizon is not first.rolling.horizon for c in candidates):
        raise ValueError(_ERR_HORIZON)
    if any(
        c.category.mode is not first.category.mode or c.category.as_of != first.category.as_of
        for c in candidates
    ):
        raise ValueError(_ERR_PIT)
    matches = [c for c in candidates if _candidate_id(c) == target]
    if len(matches) != 1:
        raise ValueError(_ERR_TARGET_PRESENT)
    target_candidate = matches[0]
    if type(target_candidate.category) is not TefasFundCategoryObservation:
        raise ValueError(_ERR_TARGET_CATEGORY)
    if not target_candidate.rolling.points:
        raise ValueError(_ERR_TARGET_ROLLING)
    return target_candidate


def _derive(
    target: UUID, candidates: tuple[TefasFundPeerCandidate, ...]
) -> tuple[tuple[TefasFundPeerMember, ...], tuple[UUID, ...], tuple[UUID, ...]]:
    target_candidate = _require_consistent(target, candidates)
    label = target_candidate.category.category_label  # type: ignore[union-attr]
    end = target_candidate.rolling.points[-1].end_date
    month = (end.year, end.month)
    members: list[TefasFundPeerMember] = []
    unclassified: list[UUID] = []
    unavailable: list[UUID] = []
    for c in candidates:
        if type(c.category) is not TefasFundCategoryObservation:
            unclassified.append(_candidate_id(c))
            continue
        if c.category.category_label != label:
            continue
        if not c.rolling.points:
            unavailable.append(_candidate_id(c))
            continue
        latest = c.rolling.points[-1].end_date
        if (latest.year, latest.month) != month:
            unavailable.append(_candidate_id(c))
            continue
        members.append(TefasFundPeerMember(category=c.category, rolling=c.rolling))
    return tuple(members), tuple(unclassified), tuple(unavailable)


@dataclass(frozen=True)
class TefasFundPeerCrossSection:
    """Same-category, same-PIT, same-horizon, same-month peer cross-section with explicit coverage gaps."""
    target_instrument_id: UUID
    candidates: tuple[TefasFundPeerCandidate, ...]
    members: tuple[TefasFundPeerMember, ...]
    unclassified_instrument_ids: tuple[UUID, ...]
    unavailable_same_category_instrument_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if type(self.target_instrument_id) is not UUID:
            raise TypeError(_ERR_TARGET_TYPE)
        candidates = _require_candidates(self.candidates)
        if type(self.members) is not tuple or any(type(m) is not TefasFundPeerMember for m in self.members):
            raise TypeError(_ERR_MEMBERS_TYPE)
        for ids in (self.unclassified_instrument_ids, self.unavailable_same_category_instrument_ids):
            if type(ids) is not tuple or any(type(i) is not UUID for i in ids):
                raise TypeError(_ERR_IDS_TYPE)
        members, unclassified, unavailable = _derive(self.target_instrument_id, candidates)
        keys = [_candidate_id(c).int for c in candidates]
        if keys != sorted(keys):
            raise ValueError(_ERR_ORDER)
        if (
            self.members != members
            or self.unclassified_instrument_ids != unclassified
            or self.unavailable_same_category_instrument_ids != unavailable
        ):
            raise ValueError(_ERR_MATCH)

    @property
    def _target(self) -> TefasFundPeerMember:
        for m in self.members:
            if m.instrument_id == self.target_instrument_id:
                return m
        raise AssertionError("target is always a member")  # pragma: no cover

    @property
    def category_label(self) -> str:
        return self._target.category.category_label

    @property
    def horizon(self) -> object:  # exact Horizon; type not imported to keep private imports minimal
        return self.candidates[0].rolling.horizon

    @property
    def mode(self) -> object:  # exact MarketDataResolutionMode; type not imported (see horizon)
        return self.candidates[0].category.mode

    @property
    def as_of(self) -> datetime | None:
        return self.candidates[0].category.as_of

    @property
    def evaluation_month(self) -> tuple[int, int]:
        return self._target.evaluation_month

    @property
    def peer_count(self) -> int:
        return len(self.members)

    @property
    def is_complete(self) -> bool:
        return not self.unclassified_instrument_ids and not self.unavailable_same_category_instrument_ids


def build_tefas_fund_peer_cross_section(
    *,
    target_instrument_id: UUID,
    candidates: tuple[TefasFundPeerCandidate, ...],
) -> TefasFundPeerCrossSection:
    """Build the peer cross-section from candidates in any order; output is canonically ordered."""
    if type(target_instrument_id) is not UUID:
        raise TypeError(_ERR_TARGET_TYPE)
    ordered = tuple(sorted(_require_candidates(candidates), key=lambda c: _candidate_id(c).int))
    members, unclassified, unavailable = _derive(target_instrument_id, ordered)
    return TefasFundPeerCrossSection(
        target_instrument_id=target_instrument_id,
        candidates=ordered,
        members=members,
        unclassified_instrument_ids=unclassified,
        unavailable_same_category_instrument_ids=unavailable,
    )


def _midrank_percentile(cross_section: TefasFundPeerCrossSection, instrument_id: UUID) -> Decimal:
    subject = None
    for m in cross_section.members:
        if m.instrument_id == instrument_id:
            subject = m
            break
    if subject is None:
        raise ValueError(_ERR_NOT_MEMBER)
    value = subject.latest_return
    less = sum(1 for m in cross_section.members if m.latest_return < value)
    equal = sum(1 for m in cross_section.members if m.latest_return == value)
    ctx = _percentile_context()
    try:
        numerator = ctx.multiply(Decimal(50), Decimal(2 * less + equal))
        return ctx.divide(numerator, Decimal(len(cross_section.members)))
    except decimal.Overflow:
        raise ValueError(_ERR_RANGE) from None


@dataclass(frozen=True)
class TefasFundPeerPercentile:
    """Midrank percentile (0-100) of one member's latest rolling return within its peer cross-section."""
    cross_section: TefasFundPeerCrossSection
    instrument_id: UUID
    percentile: Decimal

    def __post_init__(self) -> None:
        if type(self.cross_section) is not TefasFundPeerCrossSection:
            raise TypeError(_ERR_CROSS_SECTION_TYPE)
        if type(self.instrument_id) is not UUID:
            raise TypeError(_ERR_INSTRUMENT_TYPE)
        if type(self.percentile) is not Decimal:
            raise TypeError(_ERR_PERCENTILE_TYPE)
        if not self.percentile.is_finite() or not (Decimal(0) <= self.percentile <= Decimal(100)):
            raise ValueError(_ERR_PERCENTILE_RANGE)
        if self.percentile != _midrank_percentile(self.cross_section, self.instrument_id):
            raise ValueError(_ERR_PERCENTILE_MATCH)

    @property
    def is_complete(self) -> bool:
        return self.cross_section.is_complete

    @property
    def peer_count(self) -> int:
        return self.cross_section.peer_count


def calculate_tefas_fund_peer_percentile(
    *,
    cross_section: TefasFundPeerCrossSection,
    instrument_id: UUID,
) -> TefasFundPeerPercentile:
    """Compute the midrank percentile of a member's latest rolling return within its peer cross-section."""
    if type(cross_section) is not TefasFundPeerCrossSection:
        raise TypeError(_ERR_CROSS_SECTION_TYPE)
    if type(instrument_id) is not UUID:
        raise TypeError(_ERR_INSTRUMENT_TYPE)
    return TefasFundPeerPercentile(
        cross_section=cross_section,
        instrument_id=instrument_id,
        percentile=_midrank_percentile(cross_section, instrument_id),
    )
