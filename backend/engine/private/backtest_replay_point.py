"""
backend/engine/private/backtest_replay_point.py
===============================================
Pure canonical historical replay point for the Phase 26 backtest architecture (Phase 26A1). One immutable pairing of the CLOSED `AnalysisPITContext` (the
single knowledge frontier and the PIT perspective, SOURCE_AS_OF or SYSTEM_AS_OF) with an explicit economic `evaluation_date`.

Three dimensions stay distinct: the knowledge frontier (`pit_context.knowledge_cutoff`: what may be known), the PIT perspective (`pit_context.mode`) and the
economic evaluation date (explicit, never derived from the cutoff and never ordered against it: a source may announce a future-effective state in advance).
Any future execution or outcome time is outside this module and never inferred from these.

The supplied context is stored by identity. `portfolio_recorded_cutoff` is the original cutoff object: portfolio history is always bounded by
`recorded_at <= knowledge_cutoff`, also under SOURCE_AS_OF (the ledger has no source-as-of perspective). There is deliberately no market-data enum bridge here
(market_data/models.py is outside the pure dependency graph); that bridge is a later non-pure adapter. No clock, randomness, hash, I/O, loop or sequence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.domain import AsOfMode

_ERR_CONTEXT = "pit_context must be an exact AnalysisPITContext instance"
_ERR_DATE = "evaluation_date must be an exact date (a datetime is not accepted)"


@dataclass(frozen=True)
class PrivateBacktestReplayPoint:
    """The knowledge frontier and PIT perspective (the PIT context) together with the explicit economic evaluation date."""
    pit_context: AnalysisPITContext
    evaluation_date: date

    def __post_init__(self) -> None:
        if type(self.pit_context) is not AnalysisPITContext:
            raise TypeError(_ERR_CONTEXT)
        if type(self.evaluation_date) is not date:
            raise TypeError(_ERR_DATE)

    @property
    def knowledge_cutoff(self) -> datetime:
        return self.pit_context.knowledge_cutoff

    @property
    def knowledge_cutoff_utc(self) -> datetime:
        return self.pit_context.knowledge_cutoff_utc

    @property
    def as_of_mode(self) -> AsOfMode:
        return self.pit_context.mode

    @property
    def portfolio_recorded_cutoff(self) -> datetime:
        return self.pit_context.knowledge_cutoff


def build_private_backtest_replay_point(*, pit_context: AnalysisPITContext, evaluation_date: date) -> PrivateBacktestReplayPoint:
    """Build one replay point from an explicit PIT context and an explicit evaluation date; nothing is defaulted or derived."""
    return PrivateBacktestReplayPoint(pit_context=pit_context, evaluation_date=evaluation_date)
