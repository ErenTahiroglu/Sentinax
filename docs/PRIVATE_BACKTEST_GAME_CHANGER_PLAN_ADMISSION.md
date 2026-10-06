# Private backtest Game Changer plan admission (Phase 26D5B)

`backend/engine/private/backtest_game_changer_plan_admission.py` is an **admission** layer. Over a completed canonical D5A rebalance plan it answers one question: does any instrument-scoped Phase 23
Game Changer gate prohibit the NEW CAPITAL deployment actually present in this plan? Admission happens after canonical rebalance planning; the Game Changer never mutates the target, bands or friction, never
removes or edits a trade, and nothing is recomputed (no gate, no Phase 21 plan).

## Contract

`build_private_backtest_game_changer_plan_admission(*, game_changer_replay, rebalance_replay)` returns a frozen `PrivateBacktestGameChangerPlanAdmission` with exactly two fields, the D1 replay and the D5A replay (both
kept by identity). Everything else is a derived read-only view: `admission_state` (`NOT_BLOCKED` / `BLOCKED_NEW_CAPITAL`), `blocked_buy_trades` (the exact plan trade objects in plan order), `blocked_instrument_ids`
(first-occurrence plan order, each once) and `blocking_gates` (the exact D1 gates in D1 order that block at least one BUY of this plan). Nothing is stored, so nothing can be forged.

## Shared anchors

D1 and D5A must share the very analysis context object and the very portfolio projection binding object, with the same `owner_id`; value equality is not enough (an equal-valued clone context or an equal-valued but
separate binding is rejected). They use separate C2E manifests on purpose (D1: portfolio history plus Game Changer; D2/D3: other surfaces), and separate coverage wrappers over the same binding are accepted. D4B's own
internal proof is not reopened.

## Semantics

- **New capital means both BUY stages.** CASH_FUNDED_BUY and SALE_FUNDED_BUY both increase an instrument's exposure, so both are subject to the gate. Treating only external cash as new capital would let a quarantined
  instrument B be funded by selling A and routing the proceeds through SALE_FUNDED_BUY; the closed instrument-scoped gate must not be bypassed that way. The stage set is exactly these two members, not "anything but SELL".
- **SELL is not blocked by the new-capital rule.** A sale does not deploy new capital to the sold instrument. This is not a sell recommendation: the sale exists only because Phase 21B produced it, and the Game
  Changer neither ordered nor recommended it.
- **Blocking gates:** an instrument gate of PAUSED_PENDING_EVIDENCE or QUARANTINED blocks both BUY stages to its exact affected ids. OPEN imposes no pause (and is not approval or a recommendation); the closed gate is the
  only authority, so materiality, urgency, thesis and reasons are never read.
- **review alone does not block:** review REQUIRED with an OPEN capital gate is NOT_BLOCKED. The D1 gates stay reachable for review workflows.
- **SYSTEMIC events never widen:** NOT_APPLICABLE_SYSTEMIC never blocks and is never read as all instruments, the portfolio or all buys.
- **Overlap:** any blocking family blocks (OPEN plus QUARANTINED, OPEN plus PAUSED, QUARANTINED plus PAUSED block; OPEN plus OPEN does not). There is no precedence, latest-wins or no severity aggregation: every blocking gate is
  listed separately, and a trade hit by several gates appears once.
- **Not blocked cases:** a SELL-only instrument; a no-trade plan (even with a QUARANTINED gate: no capital is deployed); a quarantined target without an actual BUY (the target is never inspected for hypothetical trades);
  an affected instrument absent from the plan. Notional magnitude does not matter; any positive BUY is subject to the gate.

## Whole-plan block, no partial execution

If even one BUY is blocked the whole plan is BLOCKED_NEW_CAPITAL. The Phase 21B plan is one coupled canonical solution (funding identity, destination feasibility, minimum-sale and friction tie resolution), so D5B exposes no
allowed, filtered or executable trade subset and implies no partial execution ("run the sells but skip the blocked buys" is explicitly not a policy): no instruction is removed, no funding recomputed, no capital
redirected, no replacement plan generated. The retained plan stays unchanged.

## Claim limits

NOT_BLOCKED is not approval: it does not mean recommended, safe, suitable, target accepted, execution authorized or that no human review is required. It means only that this exact completed plan has no BUY prohibited by the
supplied historical Game Changer gates. The result is counterfactual like its inputs (D2 sleeves, D3B cash, D4B authority and D5A band/friction parameters are fixed replay parameters). no execution: no order, broker
call, settlement, ledger write or persistence, and no performance or walk-forward evaluation.

D6 may sequence completed D5B admissions against a PrivateBacktestReplayPlan by exact replay-point identity and one explicit horizon (`docs/PRIVATE_BACKTEST_DECISION_REPLAY_SEQUENCE.md`).
D6 does not turn NOT_BLOCKED into approval and does not execute admitted plans.
