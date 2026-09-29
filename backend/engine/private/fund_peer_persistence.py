"""
backend/engine/private/fund_peer_persistence.py
===============================================
Category-relative persistence over a historical sequence of PIT peer percentiles for the Private Investment
Decision Engine (Phase 16I).

Architectural Invariants:
    - Pure domain module. Zero network, filesystem, database, ambient clock, UUID generation, randomness, hashing,
      float arithmetic, pandas/numpy/scipy, persistence, or provider/resolver calls. Phase 16H
      (`TefasFundPeerPercentile`) is the sole input authority; nothing upstream is re-resolved or rewritten.
    - Each observation must come from an independently constructed historical Phase 16H cross-section, so the
      contemporaneous category at that cutoff defined its peer group. No current category is applied backward.
      Every observation must carry a non-None timezone-aware `as_of` (SYSTEM_AS_OF history); a CURRENT_REPORTED
      percentile (`as_of is None`) is not historical evidence.
    - All observations target the same instrument (both `percentile.instrument_id` and the cross-section target)
      and share one rolling horizon (by identity). The category label may change across history; persistence is
      relative standing inside the contemporaneous peer category, not category stability.
    - Canonical order is evaluation month ascending; months are unique and `as_of` strictly increases with them.
      The builder canonicalises arbitrary input order; the constructor requires canonical order.
    - persistence_score = count(percentile > 50) / count(observations), a FRACTION in [0, 1] (never x100).
      A percentile of exactly 50 does not count. Computed in an explicit fresh 50-digit ROUND_HALF_EVEN context with
      maximum exponent range; the `> 50` comparison is exact (no rounding). No minimum-history policy: one valid
      observation is defined, and `observation_count` is exposed for downstream sufficiency decisions.
    - Coverage debt is explicit, never repaired: calendar months absent between the first and last evaluation month
      are counted in `missing_month_count` (no interpolation, carry-forward or synthetic percentile); observations
      whose cross-section is partial stay in the denominator and are counted in `partial_cross_section_count`.
      `is_complete` is relative to the supplied sequence only; upstream candidate-universe completeness is not proven.
    - The constructor recomputes the score through the same private helper as the builder and rejects forgery.
    - No percentile dispersion (statistic not yet defined), qualitative labels, score bands, ranks, or
      recommendations.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from backend.engine.private.fund_peer_percentile import TefasFundPeerPercentile

_UPPER_HALF_THRESHOLD = Decimal(50)

_ERR_INSTRUMENT_TYPE = "instrument_id must be an exact UUID instance"
_ERR_PERCENTILES_TYPE = "percentiles must be a tuple of exact TefasFundPeerPercentile instances"
_ERR_EMPTY = "TEFAS peer persistence requires at least one historical percentile observation"
_ERR_TARGET = "TEFAS peer persistence observations must target the same instrument"
_ERR_HORIZON = "TEFAS peer persistence observations must share one rolling horizon"
_ERR_SYSTEM_AS_OF = "TEFAS peer persistence requires SYSTEM_AS_OF historical observations"
_ERR_DUPLICATE = "TEFAS peer persistence cannot contain duplicate evaluation months"
_ERR_ORDER = "TEFAS peer persistence observations must be in canonical evaluation-month order"
_ERR_AS_OF = "TEFAS peer persistence as_of values must increase with evaluation months"
_ERR_SCORE_TYPE = "persistence_score must be an exact Decimal instance"
_ERR_SCORE_RANGE = "persistence_score must be finite and within [0, 1]"
_ERR_SCORE_MATCH = "persistence_score must match the canonical persistence ratio exactly"


def _persistence_context() -> decimal.Context:
    return decimal.Context(
        prec=50,
        rounding=decimal.ROUND_HALF_EVEN,
        Emin=decimal.MIN_EMIN,
        Emax=decimal.MAX_EMAX,
    )


def _month_index(month: tuple[int, int]) -> int:
    return month[0] * 12 + month[1] - 1


def _require_percentiles(percentiles: object) -> tuple[TefasFundPeerPercentile, ...]:
    if type(percentiles) is not tuple or any(type(p) is not TefasFundPeerPercentile for p in percentiles):
        raise TypeError(_ERR_PERCENTILES_TYPE)
    if len(percentiles) == 0:
        raise ValueError(_ERR_EMPTY)
    return percentiles


def _canonical_score(instrument_id: UUID, percentiles: tuple[TefasFundPeerPercentile, ...]) -> Decimal:
    """Validate a canonical historical sequence and return its persistence ratio (shared by builder/constructor)."""
    for p in percentiles:
        if p.instrument_id != instrument_id or p.cross_section.target_instrument_id != instrument_id:
            raise ValueError(_ERR_TARGET)
    horizon = percentiles[0].cross_section.horizon
    if any(p.cross_section.horizon is not horizon for p in percentiles):
        raise ValueError(_ERR_HORIZON)
    for p in percentiles:
        as_of = p.cross_section.as_of
        if as_of is None or as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError(_ERR_SYSTEM_AS_OF)
    months = [_month_index(p.cross_section.evaluation_month) for p in percentiles]
    if len(set(months)) != len(months):
        raise ValueError(_ERR_DUPLICATE)
    if months != sorted(months):
        raise ValueError(_ERR_ORDER)
    as_ofs = [p.cross_section.as_of for p in percentiles]
    if any(later <= earlier for earlier, later in zip(as_ofs, as_ofs[1:])):
        raise ValueError(_ERR_AS_OF)
    upper = sum(1 for p in percentiles if p.percentile > _UPPER_HALF_THRESHOLD)
    return _persistence_context().divide(Decimal(upper), Decimal(len(percentiles)))


@dataclass(frozen=True)
class TefasFundPeerPersistence:
    """Fraction of historical PIT peer-percentile observations strictly above the peer median."""
    instrument_id: UUID
    percentiles: tuple[TefasFundPeerPercentile, ...]
    persistence_score: Decimal

    def __post_init__(self) -> None:
        if type(self.instrument_id) is not UUID:
            raise TypeError(_ERR_INSTRUMENT_TYPE)
        percentiles = _require_percentiles(self.percentiles)
        if type(self.persistence_score) is not Decimal:
            raise TypeError(_ERR_SCORE_TYPE)
        expected = _canonical_score(self.instrument_id, percentiles)
        if not self.persistence_score.is_finite() or not (Decimal(0) <= self.persistence_score <= Decimal(1)):
            raise ValueError(_ERR_SCORE_RANGE)
        if self.persistence_score != expected:
            raise ValueError(_ERR_SCORE_MATCH)

    @property
    def horizon(self) -> object:  # exact Horizon; type not imported to keep private imports minimal
        return self.percentiles[0].cross_section.horizon

    @property
    def start_month(self) -> tuple[int, int]:
        return self.percentiles[0].cross_section.evaluation_month

    @property
    def end_month(self) -> tuple[int, int]:
        return self.percentiles[-1].cross_section.evaluation_month

    @property
    def observation_count(self) -> int:
        return len(self.percentiles)

    @property
    def upper_half_count(self) -> int:
        return sum(1 for p in self.percentiles if p.percentile > _UPPER_HALF_THRESHOLD)

    @property
    def partial_cross_section_count(self) -> int:
        return sum(1 for p in self.percentiles if p.is_complete is False)

    @property
    def missing_month_count(self) -> int:
        span = _month_index(self.end_month) - _month_index(self.start_month) + 1
        return span - len(self.percentiles)

    @property
    def is_monthly_contiguous(self) -> bool:
        return self.missing_month_count == 0

    @property
    def is_complete(self) -> bool:
        return self.missing_month_count == 0 and self.partial_cross_section_count == 0


def calculate_tefas_fund_peer_persistence(
    *,
    instrument_id: UUID,
    percentiles: tuple[TefasFundPeerPercentile, ...],
) -> TefasFundPeerPersistence:
    """Build persistence from historical Phase 16H percentiles in any order; output is month-ordered."""
    if type(instrument_id) is not UUID:
        raise TypeError(_ERR_INSTRUMENT_TYPE)
    ordered = tuple(sorted(
        _require_percentiles(percentiles),
        key=lambda p: _month_index(p.cross_section.evaluation_month),
    ))
    return TefasFundPeerPersistence(
        instrument_id=instrument_id,
        percentiles=ordered,
        persistence_score=_canonical_score(instrument_id, ordered),
    )
