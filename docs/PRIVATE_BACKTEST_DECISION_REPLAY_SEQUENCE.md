# Private backtest decision replay sequence (Phase 26D6)

`backend/engine/private/backtest_decision_replay_sequence.py` is the Phase 26 sequence/provenance closure boundary. The Phase 26B `PrivateBacktestReplayPlan` (an explicit ordered tuple of replay points) is finally
consumed: it is bound to exactly one completed D5B `PrivateBacktestGameChangerPlanAdmission` per replay point, giving one ordered immutable decision-replay sequence. D6 adds no economic policy and recomputes
nothing; no D1 to D5B builder is called and no replay point or date is generated (the plan is the sole point-list authority).

## Contract

`build_private_backtest_decision_replay_sequence(*, replay_plan, horizon, admissions)` returns a frozen `PrivateBacktestDecisionReplaySequence` with exactly three fields: the plan, one explicit `Horizon` and the admissions
tuple. All are retained by identity (the tuple is not copied, sorted or rebuilt; each admission is the caller's object). Exact types only: no list, set, generator or subclass; a raw D5A or D1 result is not a D5B admission.

- **Exactly one admission per point:** `len(admissions) == len(replay_plan.points)`; no missing, extra or partial sequence.
- **Positional replay-point object identity:** for every index the admission's analysis context (through its D1 side) must carry the very replay-point OBJECT at that plan position. Equal-valued clones, another plan's
  equal point, and the same evaluation date at another cutoff do not match; several points may legitimately share an evaluation date.
- **Caller order is authoritative:** no sorting, no matching by date or cutoff, no searching, no deduplication, no repair; a reversed or permuted tuple fails.
- **explicit Horizon:** one `Horizon` for the whole sequence, never inferred from a result, the evaluation date, point spacing, a goal date or portfolio state. Every admission's analysis-context horizon must be that
  member; a mixed run fails and another horizon needs another sequence. No second `AsOfMode` parameter exists: the plan enforces one mode and each admission sits on its exact plan point.
- **Direct construction** re-runs the same validation; there are no stored derived fields to forge.

## What the sequence is, and is not

- **BLOCKED_NEW_CAPITAL and NOT_BLOCKED are both valid members**, preserved exactly. This is an audit sequence, not execution control: a blocked point is not rejected, dropped, or allowed to stop later points (no early stop,
  no halted semantics). NOT_BLOCKED is not approval, success or profit. There is no aggregate verdict, rate, worst or best state, or pass/fail score.
- **no execution and no state propagation:** the trades of admission N are never applied to point N+1; no ledger, quantity, cash, friction or post-trade value is mutated or carried forward; no `PortfolioTransaction` or
  hypothetical fill exists. Each point's own verified historical ledger projection remains its portfolio authority, and adjacent points are not checked for transaction continuity.
- **no performance and no walk-forward:** no return, profit, loss, P&L, Sharpe, Sortino, drawdown, alpha, beta, benchmark, hit rate, turnover or attribution; this is not a strategy performance backtest.
- **No policy-stability claim:** the D2 sleeves, D3B cash allocations, D4B `CrossUniverseAuthority`, and D5A band and friction parameters of different points are never compared, required equal or rejected for varying,
  because no historical strategy-configuration authority exists. The sequence is pointwise counterfactual decision replay and not proof of one historically configured strategy run through time; a future true
  walk-forward would need explicit, versioned strategy-policy provenance.
- **Surfaces not consumed:** C2E supports macro, risk-evidence and user-view categories, but the closed D1 to D5B chain consumes none of them; D6 neither adds nor requires them, and they remain outside this replay contract.

## Known Game Changer zero-event limitation

D1 requires at least one explicit Game Changer family. The architecture has family revision coverage but no source-wide, historical event-discovery coverage proving that zero event families existed at a replay cutoff.
Phase 26 therefore does not prove source-wide Game Changer event-discovery completeness, and its fully admitted sequence currently covers only replay points for which the explicit D1 contract can be constructed. An
absent D1 or D5B result never becomes "no events": there is no sentinel, no `NoGameChangerEvents` object, no synthetic OPEN or systemic gate and no benign dummy family; a point without a valid D5B admission cannot be part of a
completed sequence, and a raw D5A result must not bypass D5B. A future source-wide historical discovery and coverage authority would be required to represent a proven known-zero-event point; it is not implemented here.

## Phase 26 claim boundary

If D6 closes after independent Red Team review, the intended Phase 26 claim is narrow: deterministic point-in-time decision replay architecture is closed. It does not mean simulated execution exists, strategy performance is
backtested, walk-forward or out-of-sample evaluation exists, one fixed historical strategy configuration is proven, Game Changer source-wide zero-event coverage exists, or the omitted macro, risk and user-view surfaces were
consumed. Phase 27 (a final adversarial review of the whole closed architecture, not another policy layer) starts only after that closure.
