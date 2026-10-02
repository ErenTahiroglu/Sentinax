"""
backend/engine/private/backtest_analysis_context.py
===================================================
Pure binding of a Phase 26A1 replay point and an explicit Horizon into the canonical `AnalysisTemporalContext` (Phase 26C1). Three concepts stay separate: the
knowledge frontier and PIT perspective (the replay point's PIT context), the economic evaluation date (the replay point's evaluation_date) and the investment
horizon (an explicit caller-supplied `Horizon`, never inferred from the evaluation date, the cutoff, the distance between replay points, a goal date, portfolio
state or scheduler cadence).

Binding: the analysis PIT context IS the replay PIT context (object identity, not equality: one canonical frontier object propagates through a decision) and
the analysis horizon `as_of_date` equals the replay `evaluation_date` (never the cutoff's calendar date, and no ordering against the cutoff: a future-effective
date is valid). Direct construction revalidates both relations, so a forged context (even an equal-valued PIT copy) is rejected. One replay point may be
evaluated under several horizons. This proves temporal consistency only: no claim that any market, macro, risk, universe, event or portfolio input exists or is
complete. No input bundle, clock, randomness, hash, I/O or loop.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.engine.private.analysis_context import AnalysisTemporalContext
from backend.engine.private.analysis_horizon import AnalysisHorizonContext
from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.backtest_replay_point import PrivateBacktestReplayPoint
from backend.engine.private.domain import Horizon

_ERR_REPLAY_POINT = "replay_point must be an exact PrivateBacktestReplayPoint instance"
_ERR_TEMPORAL = "temporal_context must be an exact AnalysisTemporalContext instance"
_ERR_PIT = "temporal_context.pit_context must be the very replay_point.pit_context object"
_ERR_DATE = "temporal_context horizon as_of_date must equal replay_point.evaluation_date"


@dataclass(frozen=True)
class PrivateBacktestAnalysisContext:
    """A replay point and the analysis temporal context bound to it; everything else is reachable canonically."""
    replay_point: PrivateBacktestReplayPoint
    temporal_context: AnalysisTemporalContext

    def __post_init__(self) -> None:
        if type(self.replay_point) is not PrivateBacktestReplayPoint:
            raise TypeError(_ERR_REPLAY_POINT)
        if type(self.temporal_context) is not AnalysisTemporalContext:
            raise TypeError(_ERR_TEMPORAL)
        if self.temporal_context.pit_context is not self.replay_point.pit_context:
            raise ValueError(_ERR_PIT)
        if self.temporal_context.horizon_context.as_of_date != self.replay_point.evaluation_date:
            raise ValueError(_ERR_DATE)

    @property
    def horizon(self) -> Horizon:
        return self.temporal_context.horizon_context.horizon

    @property
    def horizon_context(self) -> AnalysisHorizonContext:
        return self.temporal_context.horizon_context

    @property
    def pit_context(self) -> AnalysisPITContext:
        return self.replay_point.pit_context


def build_private_backtest_analysis_context(*, replay_point: PrivateBacktestReplayPoint, horizon: Horizon) -> PrivateBacktestAnalysisContext:
    """Bind one replay point to an explicit canonical Horizon; the horizon is never inferred."""
    if type(replay_point) is not PrivateBacktestReplayPoint:
        raise TypeError(_ERR_REPLAY_POINT)
    horizon_context = AnalysisHorizonContext(horizon=horizon, as_of_date=replay_point.evaluation_date)
    temporal_context = AnalysisTemporalContext(horizon_context=horizon_context, pit_context=replay_point.pit_context)
    return PrivateBacktestAnalysisContext(replay_point=replay_point, temporal_context=temporal_context)
