# Game Changer Revision Family (Phase 23C1)

| Checkpoint | Scope | Status |
|---|---|---|
| 23A | Event evidence + PIT binding (`docs/GAME_CHANGER_EVENT_ENGINE.md`) | closed |
| 23B | Explicit materiality / urgency / thesis assessment (`docs/GAME_CHANGER_MATERIALITY_ASSESSMENT.md`) | closed |
| 23C1 | Revision-family completeness + active-assessment resolution | implemented (this document) |
| 23C2 | Quarantine / thesis-review / new-capital gate | deferred |
| 23D | KAP / provider ingestion + structured extraction | deferred |

23A stores explicit revision lineage and 23B assesses exactly one bound event. Neither decides which assessment is currently
authoritative inside a revision family. 23C1 answers only: given a caller-supplied family at **one PIT frontier**, is coverage explicitly
complete, and if so which exact 23B assessment is the active terminal one? It decides nothing about quarantine, new capital, alerts,
allocation or trades.

Module: `backend/engine/private/game_changer_revision_family.py` (pure, in the static-guard `PURE_MANIFEST`; imports only the standard
library and the 23A / 23B types).

```python
resolve_game_changer_revision_family(*, assessments, coverage, coverage_provenance_sha256) -> GameChangerRevisionFamilyResolution
```

## Shared PIT context and family identity

Every assessment's binding context must be the **same object** (identity, not equal values): no mixed modes, cutoffs or context objects.
PIT admissibility is not re-run; 23A stays its authority. Members must share `source_key`, `source_tier`, `scope` and `instrument_ids`;
`source_event_key` values are unique. `event_type`, effective / published / observed times, hashes, methodology and all assessed fields
may change between revisions.

## Complete vs incomplete coverage and provenance

`GameChangerRevisionFamilyCoverage.COMPLETE_AT_CUTOFF` is an explicit caller assertion that the tuple is the full known chain at the
cutoff; `INCOMPLETE_AT_CUTOFF` says completeness cannot be proven. Coverage is never inferred. `coverage_provenance_sha256` is an opaque
64-lowercase-hex reference to the artifact behind the assertion: nothing is hashed, it proves no completeness and need not equal any
event or assessment hash.

- **Incomplete**: status `INCOMPLETE_COVERAGE`, `active_assessment is None`. The evidence tuple is preserved and may lack lineage members
  (even a missing parent, which is never fabricated). No latest, last or highest-severity member is ever chosen.
- **Complete**: the supplied tuple must itself be one canonical root-to-leaf linear chain in lineage order: the first member is an
  `ORIGINAL`; every later member revises exactly the previous member's `source_event_key` and is not an `ORIGINAL`.

## Linear authority, no branches, no sorting, no timestamp winner

A missing root or parent, a branch (`B` and `C` both revising `A`), a skipped predecessor, a second `ORIGINAL`, or non-lineage order
under complete coverage is rejected. There is no branch resolution, no sorting, no transitive repair and no timestamp, materiality,
urgency or thesis-impact winner: revision edges are the only lineage authority and the caller supplies the canonical order.

## Active = terminal revision assessment

For a valid complete family `status == RESOLVED` and `active_assessment is family.assessments[-1]` (object identity, no copy). Earlier
assessments are historical provenance only; nothing is aggregated.

- **Correction**: `ORIGINAL` CRITICAL / WEAKENED followed by a `CORRECTION` LOW / UNCHANGED resolves to the correction alone. The old
  CRITICAL assessment does not leak into the active state.
- **Withdrawal**: a `WITHDRAWAL` is not a magic `None`. When it is the terminal member its own 23B assessment is active; the original is not
  revived and no concern is cleared automatically.
- **Update**: `ORIGINAL` -> `UPDATE` -> `CORRECTION` resolves to the correction.

The family and resolution constructors share one private validator with the public function, so direct construction cannot forge a
status / active pair (an equal but distinct clone of the terminal assessment is rejected by the identity contract).

## Non-goals

```text
no quarantine, new-capital blocking, review alert, allocation or rebalance coupling (23C2)
no provider / KAP ingestion, family discovery, storage or repository access, LLM extraction (23D)
no BUY / SELL / HOLD, no scheduler, no UI
```

## Claim limit

Given a non-empty caller-supplied assessment family at one exact Phase 23A PIT context and an explicit coverage assertion, 23C1 can fail
closed on incomplete coverage and, for a proven-complete unambiguous linear revision chain, identify the exact terminal Phase 23B
assessment as the active revision-state assessment. It does not claim the provider family is objectively complete, that the latest
disclosure was independently discovered, that the assessment is correct, that the event requires portfolio action or that the asset should
be sold.
