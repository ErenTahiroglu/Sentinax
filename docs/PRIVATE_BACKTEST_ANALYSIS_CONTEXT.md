# Private Backtest Analysis Context (Phase 26C1)

Module: `backend/engine/private/backtest_analysis_context.py` (pure; registered in the PURE manifests, G1-G5 clean). It binds a Phase 26A1 replay point and an
explicit `Horizon` into the canonical `AnalysisTemporalContext`, duplicating neither architecture.

## Three separate concepts

- **knowledge frontier** (and PIT perspective): the replay point's `AnalysisPITContext` (what may be known).
- **evaluation date**: the replay point's explicit economic `evaluation_date`.
- **investment horizon**: an explicit caller-supplied canonical `Horizon` member.

## Binding

- replay PIT object IS analysis temporal PIT object: `temporal_context.pit_context is replay_point.pit_context` (identity, not equality; an equal-valued
  independently built PIT context is rejected, so one canonical frontier object propagates through a historical decision).
- replay `evaluation_date` == analysis `horizon_context.as_of_date` (never the cutoff's calendar date; no ordering against the cutoff, so a future-effective
  evaluation date is valid).
- The horizon is explicit and never inferred (not from the date, the cutoff, replay-point distance, a goal date, portfolio state or scheduler cadence).
  Non-Horizon values (strings, ints, bools, HorizonFamily, None) are rejected.

Stored fields: `replay_point` and `temporal_context` only; horizon, horizon context and PIT context are properties. Direct construction revalidates both
relations. One replay point may be evaluated under multiple horizons (the same replay and PIT objects, different `Horizon`), and one horizon may be used across
different points.

## Not here

C1 makes no data completeness claim: it does not prove that market data, macro data, risk evidence, a candidate universe, Game Changer evidence or a portfolio
exists. No risk axis is created (the existing `RiskAxisContext` and risk-evidence PIT binding compose with the result unchanged, verified in tests only), no
input bundle (Phase 26C2), no replay-plan, market-data or scheduler dependency, no decision, execution, performance, clock, randomness, hash or I/O. Remote CI
does not run the Phase 26 tests yet.
