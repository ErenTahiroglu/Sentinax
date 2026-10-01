# Allocation Candidate Universe (Phase 22B)

Phase 22A (`docs/ALLOCATION_UNIVERSE_COMPOSITION.md`) turns explicit strategic sleeves into a Phase 21 target / current-state pair.
Phase 22B answers the upstream question: for the decision being evaluated, which explicit candidate set was admissible under the
knowledge available at that time, and what source / effective-time provenance supports it?

Module: `backend/engine/private/allocation_candidate_universe.py` (pure, in the static-guard `PURE_MANIFEST`; imports only the
standard library, `AnalysisPITContext`, `AsOfMode`, `AssetClass` and the 22A `CrossAssetSleeve` type).

```python
resolve_candidate_universe(*, query: CandidateUniverseQuery, snapshots: tuple[CandidateUniverseSnapshot, ...]) -> CandidateUniverseResolution
bind_candidate_universes_to_sleeves(*, sleeves, universes) -> CandidateUniverseSleeveBinding
```

## Knowledge time vs effective time vs evaluation date

```text
knowledge time   when a universe fact was public (published_at) and / or observed by Sentinax (observed_at)
effective time   when the membership applies economically: half-open [effective_from, effective_to); effective_to None = open ended
evaluation date  the date whose candidate universe is requested
```

An announcement on date A with effectiveness on a later date E may be *known* before E but does **not** apply to evaluation dates
before E; a fact announced after the knowledge cutoff is invisible even for evaluation dates after E. No clock call exists:
the cutoff comes from the explicit `AnalysisPITContext`.

## SOURCE_AS_OF vs SYSTEM_AS_OF

```text
SOURCE_AS_OF   availability = published_at, else observed_at (deterministic fallback); usable iff availability <= cutoff
SYSTEM_AS_OF   usable iff observed_at <= cutoff and (published_at is None or published_at <= cutoff)
```

All comparisons are exact UTC-instant comparisons: equality is allowed, one microsecond later is unavailable, no tolerance.
`observed_at > cutoff` does not disqualify a public fact under SOURCE_AS_OF; it does under SYSTEM_AS_OF ("what the market could
know" versus "what Sentinax had ingested").

## Source key vs universe key, coverage, provenance

- `source_key` (provider) and `universe_key` (the exact declared universe / watchlist definition) are separate strict lowercase
  identifiers (`^[a-z0-9][a-z0-9._-]{0,63}$` and up to 128 characters): no trimming, no normalization, no URLs.
- `CandidateUniverseCoverage.COMPLETE_MEMBERSHIP`: only the upstream claim that the snapshot lists every member of that declared
  universe and regime; Phase 22B does not prove it, nor the provider methodology, nor market coverage.
- `CandidateUniverseCoverage.CURATED_CANDIDATES`: a subset / watchlist. Absence means only "not in this curated set", never global
  ineligibility; no ineligibility diagnostic exists.
- `content_sha256` is an opaque upstream content-provenance reference (64 lowercase hex). Nothing is hashed, no raw payload is
  stored, and no authenticity, legal authority, completeness or signature claim is made.
- `CandidateUniverseSnapshot` stores exactly: `source_key, universe_key, content_sha256, asset_class, coverage, effective_from,
  effective_to, published_at, observed_at, instrument_ids`. Instrument ids are canonical ascending UUIDs; an **empty** tuple is a valid
  declared state. `published_at <= observed_at` by UTC instant; naive datetimes are rejected.

## Resolution pipeline and statuses

```text
1. source_key + universe_key + asset_class match          none -> NO_SOURCE_SNAPSHOT
2. effective interval contains the evaluation date        none -> NO_EFFECTIVE_SNAPSHOT
3. PIT availability filter (BEFORE conflict adjudication) none -> NO_SNAPSHOT_AS_OF
4. latest applicable effective_from regime
5. knowledge frontier inside the regime
6. identical economic fields -> logical duplicate (earliest observed_at representative); any disagreement -> FRONTIER_CONFLICT
```

Unavailable is never an empty universe: `SELECTED` with `instrument_ids == ()` is a declared zero-member state, whereas missing
exposes `None` diagnostics. Tuple order never decides anything; resolution is permutation invariant. Physical duplicate snapshot
values are rejected as invalid input. `CandidateUniverseResolution` retains the query and snapshots by identity and recomputes the
canonical result on construction through the same private function as the resolver, rejecting forged status, selected snapshot or
collection.

## Future isolation, conflicts and corrections

Later corrections, conflicting lists or corrupted snapshots that are not yet available at the cutoff cannot change a historical
selection and cannot create `FRONTIER_CONFLICT`. Two PIT-admissible frontier snapshots that disagree on content hash, coverage,
effective interval, members or `published_at` are a conflict; no hash or UUID tie-break exists. A correction available by the cutoff
with a strictly later frontier supersedes the older state.

## No resurrection, survivorship and current-list defense

A member dropped by a newer applicable snapshot stays dropped: snapshots are never unioned. A dataset of only today's constituents is
not a historical complete universe unless upstream evidence supplies the historical effective membership; nothing is inferred from
current existence, index membership, fund category or instrument status. A snapshot effective on a later date cannot be backcast to
an earlier evaluation date (`NO_EFFECTIVE_SNAPSHOT`), even when it is already known.

## Phase 22A sleeve binding

`CandidateUniverseSleeveBinding(sleeves, universes)` proves that every positive-weight Phase 22A candidate has membership in a
`SELECTED` PIT universe. Exactly one universe per sleeve, same AssetClass sequence, all `SELECTED`, one shared evaluation date and
the **same** `AnalysisPITContext` object (no mixing of knowledge times inside one decision). A candidate absent from its universe fails
closed: no removal, renormalization, fallback or replacement. The binding never alters sleeve weights, candidates or ordering and does
not require holding all members; this is not an equal-weight engine. The 22B production module never calls the 22A builder.

## Non-goals

```text
no universe discovery, provider scraping or adapters (MSCI, TEFAS, BIST, Yahoo, Tiingo, ...); no raw payloads
no ranking, score, recommendation, expected return, optimizer, macro / technical overlay
no taxonomy, InstrumentType / category / symbol / ISIN / MIC inference; no UUID-to-type lookup (no crypto route)
no tax, lot, cost basis, quantity, order or execution; membership does not imply a trade
```

## Methodology claim limit

Given explicit source-neutral candidate-universe snapshots with explicit effective and knowledge-time provenance, Phase 22B
deterministically resolves the PIT-admissible candidate set for a declared source / universe definition and verifies that Phase 22A
positive-weight sleeve candidates belong to those selected sets. It does not claim a complete global investable universe, best
candidates, correct provider methodology, optimal selection, or a survivorship-free backtest by itself (that requires the upstream
snapshots to contain valid historical membership evidence).
