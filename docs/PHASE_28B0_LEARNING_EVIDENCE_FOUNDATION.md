# Phase 28B-0 — Learning Evidence & Preregistration Foundation

Baseline: `66f0cdd6c962b8882bdabdd461b0a5d3fb96e407`. Contract foundation only.

## 1. Status and non-authorization

- **Provisional first domain:** TEFAS TRY.
- **Provisional first task:** forward volatility research (not return prediction).
- **No training authorization.** No model, label, prediction, return/volatility computation, unit-price adjustment, lifecycle interpretation, model registry, recommendation, portfolio, rebalance or trade authority exists in `backend/engine/learning/`.
- **No live collection.** No TEFAS HTTP, no KAP scraping, no scheduling, no new credentials. Capture permission is unresolved (section 5).
- No label definition may be represented as closed or frozen. All sample-size/power fields stay `POWER_ANALYSIS_REQUIRED`.

## 2. Contracts (`backend/engine/learning/`)

| Module | Purpose |
|---|---|
| `_checks.py` | Exact-type validators, canonical JSON and SHA-256 |
| `temporal_provenance.py` | Economic date, publication time, retrieval time, capture-attempt time, data revision kept as separate axes |
| `source_authority.py` | Source id, reference, version, retrieval instant, content SHA-256, authority class, limitations, availability, licensing |
| `universe_coverage.py` | `CURATED_PILOT`, `OBSERVED_LIST`, `SOURCE_CLAIMED_COMPLETE`, `UNKNOWN`; fund-code versus canonical UUID identity |
| `observation_status.py` | Observed, not observed, explicitly unavailable, unknown, retrieval failed; origin forward versus retrospective |
| `preregistration.py` | Provisional protocol record, frozen versus unresolved decisions, mandatory power-analysis placeholders |
| `adapters.py` | Read-only mapping from existing `RawProviderSnapshotRecord` and `TefasFundPriceObservation`; the only module importing private code |

Key rules:

- A SHA-256 proves stored-content identity only, not publisher authenticity or historical publication time.
- `SOURCE_CLAIMED_COMPLETE` requires an explicit `CompletenessAttestation` citing a stored source document. HTTP success, parsing or a non-empty list yields only `OBSERVED_LIST`.
- Observation vocabulary has no "missing"/zero member. `NOT_OBSERVED_IN_RESPONSE` establishes neither economic absence nor a lifecycle event, and only `OBSERVED_IN_RESPONSE` may carry a value.
- `publication_time` requires `SOURCE_DOCUMENT_STATED` plus an evidence hash; otherwise it stays unknown. The adapter never imports a provider `published_at`.
- An old economic date never becomes a retrieval or knowledge time; the only knowledge instant exposed is `system_known_at_utc` (the retrieval instant).
- No ambient clock, entropy, I/O, network or `float` in the package (static guards G1, G2, G4, G5 run over it).

## 3. Dependency isolation

- Allowed imports: a stdlib allowlist, `backend.engine.learning.*`, and (adapters only) `backend.engine.private.storage_models` and `backend.engine.private.market_data.tefas_models`.
- Forbidden: ledger, portfolio, rebalance, trade/order, backtest, scheduler, Game Changer, providers, user-private data, ML/numeric/network libraries.
- Nothing outside `backend/engine/learning/` imports it. All enforced by `backend/tests/test_learning_boundary.py`, part of permanent CI step "Phase 28B-0 learning evidence contracts".

## 4. Known official sources (verified in research phase 28A-R2)

- SPK Yatırım Fonlarına İlişkin Esaslar Tebliği III-52.1 (consolidated text listing amendments through III-52.1.e, 1/3/2024): Art. 14 (daily unit value is the rule; Kurul may grant exceptions; Art. 14(7) value may not be calculated in extraordinary cases; Art. 14(8) distribution to unit holders is possible), Art. 28 (termination), Art. 30 (merger, conversion).
- SPK Yatırım Fonlarına İlişkin Rehber (including 28.08.2026 amendments): section 8.4 (daily reference price for funds valuing periodically).
- Takasbank TEFAS Uygulama Esasları 17.01.2020 version (price definition deadline, no price means no dealing). SPK Bulletin 2026/67 and SPK announcements on liquidation of 17.09.2026.

## 5. Uncertainties

- Takasbank General Letter No. 2154 dated 6 July 2026, with its attached 2026 comparison table, has since been obtained by the project owner. Its content was NOT reviewed or relied on in this checkpoint, and no contract here encodes any of its provisions.
- Still to be verified separately: the single consolidated TEFAS Uygulama Esasları text effective 20.07.2026 (the letter and comparison table are not that consolidated text), and the 2024 version if needed for comparison. Until verified, the consolidated 2026 requirements are NOT ESTABLISHED.
- Permission to use the public TEFAS API (terms of use, rate limits, licensing) remains unverified.
- TEFAS public publication hour, price-correction frequency and the public endpoint's terms of use, rate limits and licensing are not established. Capture permission is unresolved.
- The mechanical effect of distributions on NAV, split/consolidation examples, how suspension is flagged on KAP, and historical lifecycle reconstruction are not established.
- No complete historical universe authority exists; membership must be accumulated forward. A fund-list response never proves completeness.
- Category data on TEFAS is presented under current classification; historical categories are not point-in-time.
- Patton (2011) robust-loss membership of QLIKE/MSE was not re-verified from the primary text in this repository.

## 6. Next checkpoint requirements (not started)

Phase 28B-1 (controlled forward persistence and a curated pilot) requires separate Red-Team approval and, for automated collection, a documented source-access and operational-limit policy. It additionally needs a review of General Letter 2154 and its comparison table, verification of the consolidated 20.07.2026 Takasbank procedure, and documented permission to use the public TEFAS API.
