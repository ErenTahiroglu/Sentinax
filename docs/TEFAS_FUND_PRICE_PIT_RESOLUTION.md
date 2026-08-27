# TEFAS Fund Price Point-in-Time (PIT) Resolution Specification

## 1. Overview & Architectural Scope

The TEFAS Fund Price Point-in-Time Resolver (`PointInTimeMarketDataResolver.resolve_tefas_fund_price`) provides audit-grade, deterministic selection of historical Turkish Investment Fund unit prices (`TefasFundPriceObservation`) ingested from Takasbank/TEFAS public surfaces.

Resolution guarantees:
- **Zero Float Contamination:** All unit prices are preserved as exact, finite `Decimal` objects.
- **Zero Lookahead Leakage:** Strict separation between knowledge time (`retrieved_at`), economic trade date (`trade_date`), and backtest simulation cutoff (`as_of`).
- **UUID-Independent Authority:** Resolution decisions and cryptographic SHA-256 keys depend strictly on economic data and immutable snapshot scopes, never physical random database UUIDs.

---

## 2. Query Model & Authority

```python
@dataclass(frozen=True)
class TefasFundPriceQueryKey:
    instrument_id: UUID
    trade_date: date
    provider_symbol: Optional[str] = None
```

- **Primary Identity Authority:** `(instrument_id, trade_date)` with fixed provider `TEFAS`.
- **Diagnostic Context:** `provider_symbol` (e.g. `"MAC"`, `"NNF"`) is recorded for diagnostic lineage and preflight mismatch checks, but does not control canonical identity.
- **Title Exclusion:** The source title field (`fonUnvan`) is current-metadata-only in TEFAS historical API responses and is strictly excluded from resolution authority and normalized price observation models.
- **Upstream Identity Prerequisite:** The resolver operates strictly on pre-validated observations where canonical `instrument_id` and `currency` are already resolved; it does not perform share-class discrimination.

---

## 3. Resolution Modes

| Mode | Semantics & Behavior | Status on Missing/Naive |
| :--- | :--- | :--- |
| `CURRENT_REPORTED` | Evaluates the latest available authoritative snapshot frontier based on `max(retrieved_at)`. | Returns `NO_SNAPSHOT` if no covering snapshot exists. |
| `SYSTEM_AS_OF` | Filters candidate snapshots strictly by `retrieved_at <= as_of` BEFORE frontier conflict checks and authority selection. | Requires timezone-aware `as_of`; missing/naive returns `INVALID_TEMPORAL_LINEAGE`. |
| `SOURCE_AS_OF` | **Always returns `UNAVAILABLE_SOURCE_AS_OF`**. TEFAS public surfaces provide economic price date (`tarih`), but no microsecond-level first-publication timestamp. | Constant fail-closed status. |

---

## 4. Snapshot Target-Date Coverage Semantics

TEFAS fixed-period API requests (`periyod` in {1, 3, 6, 12, 36, 60} months) do not provide user-specified arbitrary date boundaries.

Therefore, target coverage is established **ONLY** through:
1. **Two-Sided Date Range:** `snapshot.trade_date_range` where `range_start <= target_date <= range_end`.
2. **Exact Target Observation:** Snapshot contains an observation where `obs.trade_date == target_date`.

### Non-Authority Rule for `period_months`:
- A request with `periyod=60` indicates "request up to 60 months". It does **not** guarantee that the fund existed 5 years ago, that every intermediate trading date was published, or that boundary dates are present.
- `period_months` and calendar math (`retrieved_at - 60 months`) **never prove coverage alone**. Actual returned observations and valid range boundaries govern authority.

---

## 5. Temporal Filtering & Isolation Invariants

The resolver enforces a strict temporal evaluation pipeline:
1. **Provider & Instrument Filter:** Match `provider == "TEFAS"` and `instrument_id == query.instrument_id`.
2. **Transport Success Filter:** `http_status == 200` and non-empty `payload_hash`. Failed HTTP attempts (403, 429, 500, timeouts) are discarded and cannot supersede older valid data.
3. **Target Coverage Filter:** Snapshots not covering `query.trade_date` are excluded and cannot poison resolution.
4. **Timezone Awareness Validation:** Evaluated covering snapshots with naive `retrieved_at` timestamps fail closed as `INVALID_TEMPORAL_LINEAGE`. Non-covering or failed naive snapshots are ignored and do not contaminate valid lineages.
5. **SYSTEM_AS_OF Filter:** Restrict to `retrieved_at <= as_of` before evaluating frontier conflict or observation values.

---

## 6. Correction & No-Resurrection Invariants

- **Retrospective Corrections:** If Snapshot B (`retrieved_at = T2`) revises the price for a historical trade date compared to Snapshot A (`retrieved_at = T1`):
  - `CURRENT_REPORTED` selects the revised price from Snapshot B.
  - `SYSTEM_AS_OF` with `as_of < T2` deterministically selects the original price from Snapshot A.
- **True No-Resurrection:** If the newest authoritative covering snapshot's `trade_date_range` encompasses `target_date` but either:
  - Contains no observation row for `target_date`, or
  - Contains an observation marked `INVALID_OBSERVATION`,
  the resolver returns `NO_ELIGIBLE_OBSERVATION`. It **never falls back** to an older snapshot's valid price.
- **Incremental Retention:** A newer short-period snapshot (e.g. `periyod=1` covering only recent weeks) does not cover older dates. Queries for older dates safely retain the older covering 60-month snapshot as authoritative.

---

## 7. Exact Date Semantics (No Approximation)

- The resolver strictly evaluates `trade_date == query.trade_date`.
- There is no nearest-business-day, weekend, or holiday substitution inside the resolver layer.
- Non-trading days within a covered range return `NO_ELIGIBLE_OBSERVATION`. Non-trading days outside covered ranges return `NO_SNAPSHOT`.

---

## 8. Deterministic Resolution Key

Each resolution outcome computes a cryptographic SHA-256 `resolution_key` over canonical parameters:
- `observation_type` (`TEFAS_FUND_PRICE`)
- `mode` (`CURRENT_REPORTED`, `SYSTEM_AS_OF`)
- `as_of` timestamp
- `instrument_id`
- `trade_date`
- Authoritative snapshot `payload_hash`
- `period_months` & `trade_date_range`
- Economic observation fingerprint: `(instrument_id, provider, provider_symbol, trade_date, unit_price, currency, instrument_type, status)`

The resolution key is entirely invariant to physical database UUIDs and input list ordering.
