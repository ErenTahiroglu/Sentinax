# Game Changer Decision Gate (Phase 23C2)

| Checkpoint | Scope | Status |
|---|---|---|
| 23A | PIT-safe event evidence (`docs/GAME_CHANGER_EVENT_ENGINE.md`) | closed |
| 23B | Explicit structured assessment (`docs/GAME_CHANGER_MATERIALITY_ASSESSMENT.md`) | closed |
| 23C1 | Revision-family active-state authority (`docs/GAME_CHANGER_REVISION_FAMILY.md`) | closed |
| 23C2 | Human-review + instrument new-capital gate | implemented (this document) |
| 23D | KAP / provider ingestion + structured extraction | deferred |

23C2 consumes exactly one closed `GameChangerRevisionFamilyResolution` and derives a deterministic **decision-support gate**: does this
revision-family state require human thesis review, and, for an instrument-scoped family, should Sentinax temporarily pause **new**
capital to those exact instruments?

Module: `backend/engine/private/game_changer_gate.py` (pure, in the static-guard `PURE_MANIFEST`; imports only the standard library and the
closed 23A / 23B / 23C1 types).

```python
build_game_changer_decision_gate(*, resolution: GameChangerRevisionFamilyResolution) -> GameChangerDecisionGate
```

One keyword-only input and no policy parameters: the policy below is explicit architecture authority.

## Review is not a trade; quarantine pauses new capital only

`GameChangerReviewState` (`NOT_REQUIRED`, `REQUIRED`) is a review state only and schedules or sends nothing. Quarantine means pausing **new**
capital to the exact affected instrument ids plus thesis review. **Existing holdings are untouched**: no sell, exit or liquidation, no target
weight change, no rebalance or optimizer call, no order or execution, no persistence, notification or scheduler. `OPEN` means only that this
gate imposes no new-capital quarantine; it is not BUY, approval, safety or attractiveness.

## Policy

```text
Incomplete revision coverage (no active assessment, no assessment field read):
    review REQUIRED, reasons (REVISION_COVERAGE_INCOMPLETE,)
    instrument scope -> PAUSED_PENDING_EVIDENCE        systemic scope -> NOT_APPLICABLE_SYSTEMIC

Resolved: inspect ONLY the active terminal assessment. Review reasons (enum declaration order):
    MATERIALITY_HIGH_OR_CRITICAL   materiality HIGH or CRITICAL
    URGENCY_PROMPT_OR_IMMEDIATE    urgency PROMPT or IMMEDIATE
    THESIS_WEAKENED / THESIS_INVALIDATED / THESIS_UNCERTAIN   (UNCHANGED and STRENGTHENED add none)
    no reasons -> NOT_REQUIRED, otherwise REQUIRED

Instrument new-capital gate (resolved, instrument scope) is QUARANTINED if
    A. thesis impact is INVALIDATED (any materiality), or
    B. materiality is CRITICAL and thesis impact is WEAKENED or UNCERTAIN
otherwise OPEN.
```

- **INVALIDATED thesis rule**: an explicitly invalidated thesis gets no new capital before human reassessment, even at LOW materiality. This still
  does not imply a sale.
- **CRITICAL + WEAKENED/UNCERTAIN rule**: a critical event with an adverse or unresolved thesis effect fails closed.
- **Urgency affects review, not quarantine**: LOW + UNCHANGED + IMMEDIATE is review `REQUIRED` with the gate `OPEN`.
- **Materiality alone does not quarantine**: CRITICAL + UNCHANGED or STRENGTHENED is `OPEN` with review `REQUIRED`; HIGH + WEAKENED is `OPEN` with review
  `REQUIRED`.
- **Incomplete instrument family pauses new capital** until the active revision state can be established (`PAUSED_PENDING_EVIDENCE`).

## Active terminal assessment only; historical assessments do not leak

The gate reads `status`, `active_assessment` and, only for an incomplete family, the anchor identity that 23C1 proved common to all members. It never
iterates the historical family, searches for a latest or most severe member, sorts or resolves lineage again. A CRITICAL / WEAKENED original
corrected by a LOW / UNCHANGED correction is `OPEN`; a LOW original corrected to CRITICAL / WEAKENED is `QUARANTINED`; a terminal withdrawal is judged by
its own assessment and the revision kind drives nothing.

## Systemic events never imply a portfolio-wide quarantine

A SYSTEMIC event has no instrument ids and is never read as all instruments, the entire portfolio, all sleeves or all candidate universes. Systemic
families may require review (same rules as above) but the gate is always `NOT_APPLICABLE_SYSTEMIC` and `affected_instrument_ids == ()`, even for
SYSTEMIC + CRITICAL + INVALIDATED. Any macro or tactical use of systemic events is a separate later layer.

## Result contract

`GameChangerDecisionGate(resolution, review_state, review_reasons, instrument_new_capital_gate, affected_instrument_ids)`: five stored fields, no
defaults, frozen. The resolution is retained by identity; the other four fields are recomputed from it by the same private derivation the builder uses,
so any forged state, reason tuple (including a wrong order), gate or id tuple is rejected. `affected_instrument_ids` come only from the closed
resolution authority and are never caller supplied, rebuilt or sorted.

## Non-goals

```text
no SELL / exit / liquidation / reduce-position; no target-weight mutation; no rebalance, optimizer or Phase 21 call
no execution, broker, persistence, notification or scheduler
no score, probability, confidence or arithmetic; no LLM, KAP or provider access (23D)
```

## Claim limit

Given one closed Phase 23C1 resolution, 23C2 deterministically derives whether human thesis review is required and, for explicitly instrument-scoped
families, whether Sentinax should temporarily pause new capital to those exact instruments under the declared gate policy. It does not claim that the
asset should be sold, that existing holdings should be reduced, that the event proves an investment loss, that the system recommends buying, or that a
systemic event blocks the whole portfolio.
