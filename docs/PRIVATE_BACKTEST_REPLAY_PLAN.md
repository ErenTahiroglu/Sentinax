# Private Backtest Replay Plan (Phase 26B)

Module: `backend/engine/private/backtest_replay_plan.py` (pure; registered in the PURE manifests, G1-G5 clean). A `PrivateBacktestReplayPlan(points)` is an
immutable tuple of explicit Phase 26A1 replay points. It answers only which points belong to the replay and in what knowledge-time order.

## Ordering rules

In short: caller order is kept, there is no sorting and no dedup, and there is one PIT perspective per plan.

- Plan order = `knowledge_cutoff` instant: adjacent `knowledge_cutoff_utc` values must strictly increase. Equality and reverse order are `ValueError`. The
  comparison is by UTC instant through the closed point, so `10:00+03:00` and `07:00 UTC` are the same frontier and a duplicate.
- Caller order is preserved and is the authority: no sorting, no dedup, no repair; `points` is stored by identity (exact `tuple`, no copy).
- One PIT perspective per plan: every point has the first point's `AsOfMode`; a mid-run SOURCE_AS_OF/SYSTEM_AS_OF switch is rejected (build two plans for a
  comparison).
- Non-empty (a one-point plan is valid); every member an exact `PrivateBacktestReplayPoint`; validation also runs on direct construction.

## evaluation_date is not order authority

The economic `evaluation_date` is never compared across points. It may repeat (several intraday knowledge frontiers for one economic date), decrease, or lie
after the cutoff's calendar date (a future-effective regime announced in advance), without invalidating the plan.

## Not here

Replay dates are not generated: no interpolation, no calendar, no market sessions, no scheduler recurrence (a later explicit adapter would be needed). No
market-data (A2) dependency, input resolution, execution, performance, clock, randomness, hash or I/O. The Phase 26 architecture tests are permanently wired in CI.
D6 is now the downstream consumer that binds exactly one completed D5B result to every replay-plan point in caller order (`docs/PRIVATE_BACKTEST_DECISION_REPLAY_SEQUENCE.md`).
