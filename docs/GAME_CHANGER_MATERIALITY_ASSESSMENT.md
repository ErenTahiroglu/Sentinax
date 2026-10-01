# Game Changer Materiality Assessment (Phase 23B)

| Checkpoint | Scope | Status |
|---|---|---|
| 23A | Immutable event evidence, PIT admissibility, revision metadata (`docs/GAME_CHANGER_EVENT_ENGINE.md`) | closed |
| 23B | Structured materiality / thesis impact / review urgency / impact dimensions | implemented (this document) |
| 23C | Revision-family resolution + quarantine / thesis-review allocation gate | deferred |
| 23D | KAP / provider ingestion + structured extraction boundary | deferred |

## 23A evidence vs 23B assessment

23A answers what event existed, which instruments it concerned, when it was knowable, its provenance and revision metadata.
23B takes one already PIT-admissible `GameChangerEventPITBinding` and validates and preserves an **explicit, caller-supplied,
versioned structured assessment** of that exact event. It is not a text classifier: it reads no news, KAP body, PDF, headline or LLM
output, and it contains no classification, prediction or derivation algorithm.

Module: `backend/engine/private/game_changer_assessment.py` (pure, in the static-guard `PURE_MANIFEST`; imports only the standard
library and the 23A binding type).

```python
build_game_changer_materiality_assessment(*, binding, materiality, urgency, thesis_impact, impact_dimensions,
                                          materiality_basis, methodology_key, assessment_provenance_sha256)
```

Eight stored fields, no defaults, frozen. The binding is retained by identity and must be the exact 23A binding: a raw event is
rejected so that an event not available at the cutoff cannot be assessed. Temporal admissibility remains solely the 23A authority.

## Internal materiality, not a legal determination

`GameChangerMateriality` (`LOW`, `MEDIUM`, `HIGH`, `CRITICAL`) is an internal Sentinax decision-review classification. It is not a
legal conclusion: not material information under IFRS, not inside information under MAR, not material under securities law and not a
disclosure requirement.

## No numerical materiality score

No 0-100 score, probability, percentage, ratio, monetary or market-cap threshold, price reaction or weighted sum exists; the module has
no numeric type or arithmetic. Materiality is contextual, so the vocabulary is ordinal words only. `GameChangerMaterialityBasis`
(`NATURE`, `MAGNITUDE`, `NATURE_AND_MAGNITUDE`) records only the kind of consideration the upstream assessment used and holds no magnitude.

## Orthogonal axes

- **Materiality vs urgency**: `GameChangerUrgency` (`ROUTINE`, `PROMPT`, `IMMEDIATE`) is review priority only; it schedules, notifies,
  freezes and quarantines nothing. CRITICAL does not imply IMMEDIATE and vice versa.
- **Materiality vs thesis impact**: `GameChangerThesisImpact` (`UNCHANGED`, `STRENGTHENED`, `WEAKENED`, `INVALIDATED`, `UNCERTAIN`) is the
  supplied judgement about the investment thesis. WEAKENED is not SELL, STRENGTHENED is not BUY, INVALIDATED is not liquidation. UNCHANGED
  does not imply LOW and CRITICAL does not imply INVALIDATED.
- **Materiality vs data confidence**: no confidence, freshness or coverage score exists. A highly material event may rest on uncertain
  assessment evidence and a low-materiality event may have excellent source quality.
- **No event-type, source-tier or revision-kind defaults**: the same event type accepts every materiality; `SourceTier` is evidence
  provenance, not importance; ORIGINAL, UPDATE, CORRECTION and WITHDRAWAL events determine no severity. Systemic / macro-shock events get
  no automatic CRITICAL, IMMEDIATE or MACRO_EXPOSURE.

## Impact dimensions

`GameChangerImpactDimension` lists affected analytical channels only (earnings, cash flow, balance sheet, valuation, operations,
financing / liquidity, governance, legal / regulatory, ownership / control, capital structure, macro exposure, other). They carry no
direction, magnitude, score or weight; `VALUATION` is only a label, not a price-target or expected-return effect. The tuple is non-empty,
unique and in enum declaration order (validated, never sorted; representation only, no priority meaning); `OTHER` is the explicit
"none of the named", an empty tuple never means unknown. There is no portfolio, rebalance or position dimension: portfolio consequence
belongs to 23C.

## Methodology key and assessment provenance

`methodology_key` is a strict lowercase version identity (`^[a-z0-9][a-z0-9._-]{0,127}$`, no trimming; e.g. `game_changer.materiality.v1`
is only an example). It proves no methodology quality. `assessment_provenance_sha256` is an opaque 64-lowercase-hex reference to the
upstream assessment artifact; nothing is hashed here. It is **distinct** from the event's `content_sha256`: one identifies the event
content artifact, the other the assessment artifact; equality is neither required nor forbidden and neither proves correctness. There is
no free-form rationale field: explainable detail lives in the upstream audit artifact the hash references.

## Revision resolution is deferred to 23C

23A stores revision lineage without resolving it, and 23B assesses exactly the event in the supplied binding. A correction or withdrawal
event may itself be assessed; no `active`, `superseded` or `resolved` state exists here. **Phase 23C MUST NOT naively aggregate all
historical assessments.** Before any quarantine or thesis-review gate it must determine which revision-family state is active at the
applicable PIT cutoff; otherwise a withdrawn or corrected CRITICAL assessment could block new capital forever.

## Non-goals

```text
no sentiment / polarity; no LLM or prompt runtime; no classification, scoring, ranking or prediction
no quarantine, freeze, pause, review flag or allocation consequence; no BUY / SELL / HOLD mapping
no expected-return, alpha or valuation-target effect
no provider / KAP access, scheduler, notification or UI
```

## Claim limit

Given one already PIT-admissible Phase 23A event and an explicit versioned structured assessment supplied by the caller, Phase 23B
validates and preserves its decision materiality, review urgency, thesis impact, affected dimensions, methodology identity and assessment
provenance. It does not claim that Sentinax objectively determines materiality, predicts event impact, legally determines material
information, recommends a trade or proves the assessment correct.
