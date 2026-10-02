"""
backend/engine/private/backtest_market_data_bridge.py
=====================================================
Canonical market-data temporal bridge for the Phase 26 backtest architecture (Phase 26A2). A NON-PURE dependency adapter by classification only: the canonical
`MarketDataResolutionMode` lives in market_data/models.py, which is outside the PURE dependency graph, so this module is deliberately not in the PURE
manifest. It stays deterministic and side-effect free: no I/O, clock, randomness, hash or loop.

It maps the pure Phase 26A1 replay point onto the two temporal parameters the existing market-data resolver already takes, by exact enum member identity (never
by serialized value, name or case, the two enums differ in value casing):

    AsOfMode.SOURCE_AS_OF -> MarketDataResolutionMode.SOURCE_AS_OF
    AsOfMode.SYSTEM_AS_OF -> MarketDataResolutionMode.SYSTEM_AS_OF

There is no replay mapping to the current-reported perspective and no fallback of any kind: an unavailable SOURCE_AS_OF stays a resolver outcome and is never
retried as another mode. `as_of` is the original knowledge-cutoff object of the replay point (not normalized); the economic `evaluation_date` is a different axis
and is never used as `as_of`. The bridge does not call the resolver and builds no market-data query key; consumers call the resolver explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from backend.engine.private.backtest_replay_point import PrivateBacktestReplayPoint
from backend.engine.private.domain import AsOfMode
from backend.engine.private.market_data.models import MarketDataResolutionMode

_ERR_REPLAY_POINT = "replay_point must be an exact PrivateBacktestReplayPoint instance"
_ERR_MODE = "replay point carries an unsupported AsOfMode"


@dataclass(frozen=True)
class PrivateBacktestMarketDataContext:
    """The replay point whose frontier and perspective feed the market-data resolver; nothing derived is stored."""
    replay_point: PrivateBacktestReplayPoint

    def __post_init__(self) -> None:
        if type(self.replay_point) is not PrivateBacktestReplayPoint:
            raise TypeError(_ERR_REPLAY_POINT)

    @property
    def resolution_mode(self) -> MarketDataResolutionMode:
        mode = self.replay_point.as_of_mode
        if mode is AsOfMode.SOURCE_AS_OF:
            return MarketDataResolutionMode.SOURCE_AS_OF
        if mode is AsOfMode.SYSTEM_AS_OF:
            return MarketDataResolutionMode.SYSTEM_AS_OF
        raise RuntimeError(_ERR_MODE)

    @property
    def as_of(self) -> datetime:
        return self.replay_point.knowledge_cutoff


def build_private_backtest_market_data_context(*, replay_point: PrivateBacktestReplayPoint) -> PrivateBacktestMarketDataContext:
    """Bridge one replay point to the market-data resolver's mode and as_of parameters."""
    return PrivateBacktestMarketDataContext(replay_point=replay_point)
