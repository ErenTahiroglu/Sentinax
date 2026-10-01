# Game Changer Event Engine

## Phase 23 purpose and roadmap

The Game Changer engine is **not** sentiment analysis, news polarity, BUY / SELL generation, price prediction or LLM portfolio
control. Its first responsibility is narrow: represent one external material event with canonical scope, source provenance,
knowledge time, an optional economic effective date and explicit revision lineage, without fabricating any interpretation.
Interpretation begins only after a safe evidence boundary exists.

| Checkpoint | Scope | Status |
|---|---|---|
| 23A | Canonical event evidence + PIT provenance | implemented (this document) |
| 23B | Materiality / thesis-impact assessment | deferred |
| 23C | Quarantine / thesis-review allocation gate | deferred |
| 23D | KAP / provider ingestion | deferred |
| 24 | Scheduler | deferred |

A later LLM may only perform structured extraction. It must never set portfolio weights, trigger a sale, change rebalance targets
or execute an order. Phase 23A has zero LLM dependency.

## 23A: evidence-only boundary

Module: `backend/engine/private/game_changer_event.py` (pure, in the static-guard `PURE_MANIFEST`; imports only the standard library,
`AnalysisPITContext`, `AsOfMode` and `SourceTier`).

```python
bind_game_changer_event_pit(*, event: GameChangerEvent, context: AnalysisPITContext) -> GameChangerEventPITBinding
```

### Source-neutral taxonomy

`GameChangerEventType` is a structural label (financial report, guidance, capital allocation, financing / liquidity, M&A, operations,
management / governance, legal / regulatory, ownership / control, corporate action, macro shock, other).
`event_type != materiality`, `event_type != sentiment`, `event_type != recommendation`: no type is inherently positive or negative.
`GameChangerRevisionKind` (original, update, correction, withdrawal) is explicit upstream lineage, never inferred from text.

### Instrument vs systemic scope

`INSTRUMENT` requires at least one canonical UUID (unique, ascending, resolved upstream; nothing is inferred from symbol, issuer name,
ISIN, KAP code or headline). `SYSTEMIC` requires none. `MACRO_SHOCK` is the only `SYSTEMIC` type and every other type requires
`INSTRUMENT` scope, so an unresolved company event is never silently systemic and `OTHER` is not an identity escape hatch.

### Stored event contract (twelve fields, no defaults)

`source_key` (strict lowercase identifier, at most 64 characters), `source_event_key` (provider-native immutable key kept exactly:
case preserved, trimmed, printable, 1..128 characters, never constructed from headline / date / hash), `source_tier` (explicit
`SourceTier`, never derived from the source key), `scope`, `event_type`, `instrument_ids`, `effective_date`, `published_at`,
`observed_at`, `content_sha256`, `revision_kind`, `revises_source_event_key`. The core carries no source-specific flags.

### Published vs observed vs effective time

```text
published_at    proven source-publication instant, or None (never fabricated from effective, retrieved or document dates)
observed_at     when Sentinax / the upstream pipeline observed the event (caller supplied; no clock call)
effective_date  known economic / legal effective date or None; not the publication date; may lie in the future
```

`published_at <= observed_at` by UTC instant; naive datetimes and malformed tzinfo fail closed.

### SOURCE_AS_OF vs SYSTEM_AS_OF

```text
SOURCE_AS_OF   availability = published_at, else observed_at (fallback); admissible iff availability <= knowledge_cutoff
SYSTEM_AS_OF   admissible iff observed_at <= knowledge_cutoff and (published_at is None or published_at <= knowledge_cutoff)
```

Exact UTC-instant comparison; equality is allowed and one microsecond later is rejected. An event published at 10:00Z and observed
at 12:00Z is admissible at an 11:00Z cutoff under SOURCE_AS_OF and rejected under SYSTEM_AS_OF: publicly knowable is not the same
as already observed. When `published_at` is missing, SOURCE_AS_OF falls back to `observed_at`.

### Future-effective events

An event published by the cutoff that states an action effective next month is admissible: the future effective date was already
public knowledge. `effective_date > cutoff.date()` is not look-ahead and never causes rejection.

### Content provenance and revision lineage

`content_sha256` is an opaque upstream reference (64 lowercase hex). Nothing is hashed, no raw HTML / PDF / JSON / KAP body is stored,
and the hash does not prove authenticity. `ORIGINAL` must not revise another event; `UPDATE`, `CORRECTION` and `WITHDRAWAL` must
name a different `revises_source_event_key`. The chain is not resolved or chased and no earlier event is superseded in 23A.

## Non-goals

```text
no sentiment / polarity; no materiality, urgency or thesis-impact (23B)
no quarantine, pause, review or allocation consequence (23C)
no LLM, prompt, embedding or model output
no KAP scraping, undocumented endpoints, RSS, Telegram or provider adapters (23D); KAP automated access stays conditional
no legacy Buffett MockKAPFetcher reuse
no portfolio, rebalance or allocation coupling; no tax, lot, order or execution; no scheduler or UI
```

## Methodology claim limit

Given an explicit source-neutral event with canonical scope, provenance, source / observation times and revision metadata, Phase 23A
represents the immutable event evidence and can prove whether that event was temporally admissible at one explicit Sentinax analysis
cutoff. It does not claim materiality, truth of the disclosure, economic or thesis impact, positive / negative interpretation,
correctness of source content, a recommendation or any portfolio action.
