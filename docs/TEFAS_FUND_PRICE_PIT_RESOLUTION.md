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

---

## Phase 16A — PIT-Safe TEFAS Fund Price Series (`backend/engine/private/fund_price_series.py`)

- **Purpose:** the single authoritative historical unit-price series contract that later fund analytics must consume, instead of raw TEFAS responses, latest rows, unsorted observations, future snapshots, or silently backfilled observations. It only wraps resolver output; it calculates no returns, volatility, drawdown, VaR/CVaR, Sharpe/Sortino, rankings, scores, or recommendations, and consumes none of the current-metrics fields (`portfolio_size`, `investor_count`, `outstanding_units`, reported current price, `fonKategori`).
- **`TefasFundPricePoint` (frozen, six fields):** `trade_date` (exact `date`, never `datetime`), `unit_price` (exact finite `Decimal` > 0, no float), `currency` (exact `Currency`), `confidence` (exact `DataConfidenceLevel`, taken from the selected observation), `snapshot_retrieved_at` (exact timezone-aware datetime), `resolution_key` (non-empty `str`, the resolver's existing key). The mutable source observation is not retained.
- **`TefasFundPriceGap` (frozen, two fields):** `trade_date` and `status` (any `MarketDataResolutionStatus` except `SELECTED`). A gap means the requested date produced no authoritative SELECTED observation under the requested PIT mode. It does NOT mean price zero, return zero, previous price, next price, a weekend, or a holiday.
- **`TefasFundPriceSeries` (frozen, six fields):** `instrument_id`, `mode`, `as_of`, `requested_dates` (non-empty, exact dates, strictly ascending, unique), `points`, `gaps`. Every requested date appears exactly once in `points` or `gaps`, never both, never neither, and both keep requested-date order. `is_complete` is `len(gaps) == 0`; there is no coverage percentage or minimum-observation threshold yet.
- **Builder:** `build_tefas_fund_price_series(*, instrument_id, trade_dates, snapshots, mode, as_of, provider_symbol=None)`. For each requested date it builds a `TefasFundPriceQueryKey` and calls `PointInTimeMarketDataResolver.resolve_tefas_fund_price`; it never filters snapshots, selects a latest snapshot, or re-implements resolver logic. The snapshot collection is not retained. `provider_symbol` is diagnostic only. Inputs are validated with exact types; dates are never sorted or normalized (callers cannot hide malformed ordering).
- **Modes:** `SYSTEM_AS_OF` requires an exact timezone-aware `as_of` (validated before resolution; no normalization, no current time) and inherits the resolver's future isolation: snapshots retrieved after the cutoff cannot change the series. `CURRENT_REPORTED` requires `as_of is None`. `SOURCE_AS_OF` stays unavailable: the resolver returns `UNAVAILABLE_SOURCE_AS_OF`, so every requested date becomes a gap; it is never downgraded to another mode.
- **No Resurrection (inherited):** when a newer authoritative snapshot covers a date but omits or invalidates the row, the date is a gap (`NO_ELIGIBLE_OBSERVATION`); the older price is never restored. Snapshot and observation conflicts likewise become gaps (`SNAPSHOT_CONFLICT`, `OBSERVATION_CONFLICT`).
- **Missing != Zero, No Fabrication:** no interpolation, forward-fill, backward-fill, or calendar/holiday/weekend inference; the caller supplies the explicit `trade_dates`.
- **Currency Consistency:** all points in one series must share one `Currency`; otherwise construction fails closed with `ValueError("TEFAS fund price series contains inconsistent currencies")`. No FX conversion is performed.
- **Defensive Checks:** a SELECTED result whose observation is missing, not an exact `TefasFundPriceObservation`, or does not match the requested instrument and date fails closed.
- **Guard Status:** the module is deliberately NOT in `PURE_MANIFEST`. It must call `PointInTimeMarketDataResolver`, and the `market_data` modules it imports are not pure-manifest modules (`uuid4` default factories, `datetime.now` defaults, `hashlib`), so G3 flags exactly those three imports and adding it to the manifest would fail the manifest test without a scanner change. The module itself is clean under G1, G2, G4, and G5 (asserted directly in its test suite; G4/G5 also scan the whole private tree).

---

## Phase 16B — Deterministic TEFAS Fund Return Series (`backend/engine/private/fund_return_series.py`)

- **Scope:** simple returns only. The module derives returns exclusively from an already-authoritative Phase 16A `TefasFundPriceSeries`; it never accepts raw TEFAS observations or snapshots and never calls the market-data resolver. Phase 16A remains the sole price-series authority.
- **`TefasFundReturnPoint` (frozen, three fields):** `start_date` (exact `date`), `end_date` (exact `date`, strictly after `start_date`), `simple_return` (exact finite `Decimal` greater than or equal to `-1`). Zero, positive, and negative returns down to and including `-1` are valid; `float`, `int`, `bool`, `str`, `NaN`, `Infinity`, and values below `-1` are rejected. The exact economic return from strictly positive prices is always `> -1`, but the stored 50-digit analytical approximation of an extremely near-total loss may round to exactly `-1`; that does not mean the endpoint price was zero (for example `1 -> 1E-1000000` stores `-1`). The series constructor still binds every point to the source, so a forged `-1` for an ordinary source such as `100 -> 1` (canonical `-0.99`) is rejected. Points do not copy prices, currency, resolution keys, confidence, snapshot timestamps, instrument, mode, or `as_of`; that provenance stays in the retained source.
- **`TefasFundReturnSeries` (frozen, two fields):** `source` (exact `TefasFundPriceSeries`, retained by identity) and `points`. The constructor is self-validating: `len(points) == len(source.points) - 1`, and each point must match the adjacent source pair (`start_date`, `end_date`) and equal the return recomputed from the two source prices under the Phase 16B Decimal context. Forged series (wrong dates, wrong return, missing, extra, reordered, bridging points, point or source subclasses) are rejected with static errors.
- **Complete Source Required, No Gap Bridging:** the source must satisfy `gaps == ()` and have at least two price points; otherwise `ValueError("TEFAS fund return series requires a complete price series")` or `ValueError("TEFAS fund return series requires at least two price points")`. Missing stays missing: a selected D1, a gap at D2, and a selected D3 never produce a D1 -> D3 return. A Phase 16A `SOURCE_AS_OF` series is currently all gaps and is therefore rejected; no returns are invented from unavailable source-time data. Source temporal semantics are inherited, not re-validated.
- **Arithmetic:** for adjacent authoritative prices `P0 -> P1`, `simple_return = P1 / P0 - 1`, stored as a decimal fraction (`0.10` means `+10%`, `-0.05` means `-5%`), with no percentage scaling, log returns, or annualization. Actual `start_date`/`end_date` are kept; intervals may span weekends, holidays, or other calendar jumps between consecutive authoritative points. No rescaling by elapsed days and no calendar inference.
- **Decimal Contract:** every division and subtraction uses a fresh explicit `decimal.Context` with 50 significant digits (`_RETURN_DECIMAL_PRECISION = 50`), `ROUND_HALF_EVEN`, and Decimal's maximum exponent range (`Emin = decimal.MIN_EMIN`, `Emax = decimal.MAX_EMAX`, with no smaller arbitrary bound). Valid Phase 16A prices such as `1E-1000000 -> 1` therefore produce a finite deterministic return. A result beyond even that maximum range (for example `1E-MAX_EMAX -> 1E+MAX_EMAX`) fails closed with `ValueError("TEFAS fund return exceeds supported Decimal analytics range")`; no `decimal.Overflow` escapes, and the module never returns Infinity, converts to float, uses `Fraction`, or allocates unbounded precision. This is an analytical precision contract, not money rounding, currency precision, database `NUMERIC` precision, or display precision. Repeating returns are deterministic approximations, not exact Decimals: for example `3 -> 4` is `0.3333333333333333333333333333333333333333333333333` (the 50-digit quotient minus one leaves 49 significant digits) and `3 -> 2` is `-0.33333333333333333333333333333333333333333333333333`. Economically equal price spellings (`100`, `100.0`, `100.00`, `1E+2`) give equal returns.
- **Ambient Context Isolation:** the implementation never reads or mutates `decimal.getcontext()`. Tests change the global precision and rounding mode and prove identical results and an unchanged global context, for both the builder and the constructor validation.
- **Compounding:** for `100 -> 110 -> 99`, `(1 + r1) * (1 + r2)` equals `99 / 100` in the same explicit context.
- **No Sample Policy and No Risk Metrics:** two prices (one return) are structurally sufficient; whether a window is statistically sufficient belongs to later metric layers. No mean/rolling return, CAGR, volatility, downside deviation, drawdown, VaR/CVaR, Sharpe/Sortino/Calmar, capture ratios, persistence, rankings, scores, or recommendations exist here.
- **Guard Status:** like `fund_price_series.py`, the module is NOT in `PURE_MANIFEST`, because it imports `fund_price_series.py`, which depends on the market-data resolver modules. The module itself is clean under G1, G2, G4, and G5, and G3 flags only the `fund_price_series` import (asserted directly in its test suite; G4/G5 also scan the whole private tree). No scanner exception was added.

---

## Phase 16C — TEFAS Maximum Drawdown + Recovery (`backend/engine/private/fund_drawdown.py`)

- **Scope:** maximum drawdown, peak/trough identity, recovery date, recovery duration, and underwater duration only. The module consumes only a Phase 16A `TefasFundPriceSeries`; it never accepts raw observations or snapshots, never calls the market-data resolver or any provider, and reuses no legacy analyzer or frontend math. No rolling returns, volatility, downside deviation, Sortino, Calmar, VaR/CVaR, Sharpe, rankings, or scores.
- **Complete Source Required:** the source must be an exact `TefasFundPriceSeries` with `gaps == ()` and at least two points. Any gap raises `ValueError("TEFAS fund drawdown requires a complete price series")` (including all-gap `SOURCE_AS_OF` series); fewer than two points raises `ValueError("TEFAS fund drawdown requires at least two price points")`. Missing or insufficient data is never reported as a zero drawdown; a flat or rising complete series is an observed zero drawdown. Source temporal semantics are inherited, not reinterpreted.
- **`TefasFundDrawdown` (frozen, five fields):** `source` (retained by identity), `max_drawdown`, `peak_date`, `trough_date`, `recovery_date` (`date | None`). No instrument, mode, currency, or provenance fields are copied.
- **Loss-Magnitude Semantics:** `max_drawdown` is a non-negative loss magnitude in `[0, 1]`: `drawdown_t = 1 - P_t / peak_t`, where `peak_t` is the running maximum price up through `t`. Examples: `100 -> 80` gives `0.20`, `100 -> 150` gives `0`, `100 -> 150 -> 75` gives `0.50`. It is never stored as `-0.20`.
- **Decimal Contract:** an explicit fresh `decimal.Context` (50 significant digits, `ROUND_HALF_EVEN`, `Emin = MIN_EMIN`, `Emax = MAX_EMAX`); no ambient context, no float, no quantization. An extremely small positive trough (for example `1 -> 1E-1000000`) may analytically round to exactly `1` while both prices remain strictly positive.
- **Peak and Tie Semantics:** the running peak moves only on a strictly higher price, so equal prices keep the earliest peak date. The maximum-drawdown episode updates only on a strictly deeper drawdown, so an equal-depth later episode keeps the earliest one.
- **Recovery Semantics:** recovery is the first source point strictly after `trough_date` whose price is `>= peak_price` (for `100 -> 120 -> 60 -> 120 -> 130` recovery is the first `120`, not `130`). If none exists, `recovery_date is None`; recovery is never fabricated at the analysis end. A zero drawdown reports the first point's date as peak, trough, and recovery.
- **Durations:** `decline_days = trough_date - peak_date`, `recovery_days = recovery_date - trough_date`, `underwater_days = recovery_date - peak_date`, all in calendar days with no trading-day inference. `recovery_days` and `underwater_days` are `None` when unrecovered (no fake duration from the final observation date) and `0` for a zero drawdown.
- **Self-Validation:** the constructor validates exact field types and ranges (`0 <= max_drawdown <= 1`, `peak_date <= trough_date`, `recovery_date >= trough_date`), then recomputes the canonical result from `source` through the same private helper used by the builder and requires exact agreement, rejecting forged drawdowns, dates, or recovery. Errors are static.
- **Guard Status:** like the other Phase 16 modules, this module is NOT in `PURE_MANIFEST`, because it depends on `fund_price_series.py`, which depends on the market-data resolver modules. It is clean under G1, G2, G4, and G5, and G3 flags only the `fund_price_series` import (asserted directly in its test suite; G4/G5 also scan the whole private tree). No scanner exception was added.

---

## Phase 16D — Monthly Rolling 12M / 36M / 60M TEFAS Returns (`backend/engine/private/fund_rolling_returns.py`)

- **Scope:** month-end rolling cumulative returns for exactly three horizons: `Horizon.ALLOCATION_12M` (12 months), `Horizon.STRATEGIC_3Y` (36), and `Horizon.STRATEGIC_5Y` (60). Any other `Horizon` raises `ValueError`; raw ints, strings, and foreign enums raise `TypeError`. No new horizon enum exists. Only a complete Phase 16A `TefasFundPriceSeries` is consumed; no raw observations, resolver, or provider calls.
- **Month-End Observation Convention:** the representative of a calendar month is the LAST authoritative source point in that month (no averaging, first-of-month, nearest-date, or interpolation). A window compares the end month's representative with the representative of the month exactly `horizon.months` earlier, so `2025-09-29 -> 2026-09-30` is a valid 12M window; equal day-of-month is not required.
- **Calendar-Month Matching:** months are matched by `(year, month)` identity through an exact integer month subtraction. There is no 365-day, 365.25-day, `timedelta`, pandas `DateOffset`, or `dateutil` logic, so February and leap years match naturally (`2024-02-29 -> 2025-02-28`).
- **Monthly Continuity:** from the month of the first source point through the month of the last, every calendar month must contain at least one source point; otherwise `ValueError("TEFAS rolling returns require continuous monthly price coverage")`. A missing month is never bridged. This is NOT daily or trading-day completeness and involves no holiday inference. A source with explicit Phase 16A gaps fails first with `ValueError("TEFAS rolling returns require a complete price series")`, and fewer than two points fails with `ValueError("TEFAS rolling returns require at least two price points")`. A `SOURCE_AS_OF` series (all gaps) therefore cannot reach the rolling calculation.
- **Windows and Insufficient History:** for `M` continuous months and a horizon of `H` months the window count is `max(M - H, 0)` (13 months gives one 12M window, 12 months gives none; 37 gives one 36M, 61 gives one 60M). End months whose start month precedes the first represented month produce no window. This is not missing data: an empty `points` tuple is VALID (`is_available` is `False`, `window_count` is `0`), and no window is fabricated.
- **Types:** `TefasFundRollingReturnPoint(start_date, end_date, simple_return)` (exact dates, `end_date > start_date`, exact finite `Decimal >= -1`) and `TefasFundRollingReturnSeries(source, horizon, points)`, which retains the source by identity. Points copy no instrument, currency, mode, `as_of`, price, confidence, or resolution key. The series constructor recomputes the canonical windows from the source through the same private helper as the builder and rejects forged start/end dates, returns, counts, ordering, or horizon.
- **Arithmetic Authority:** the Phase 16B `_simple_return` is the only return arithmetic (`P1 / P0 - 1` in the explicit 50-digit `ROUND_HALF_EVEN` context with maximum exponent range). Nothing is duplicated; a rounded analytical `-1` stays valid for the same reason as Phase 16B, and `TEFAS fund return exceeds supported Decimal analytics range` propagates unchanged. Output is independent of, and never mutates, the ambient Decimal context.
- **Cumulative, Not Annualized:** stored values are cumulative window returns. There is no CAGR or annualization.
- **Not Included:** mean/median rolling return, win rate, peer percentile, persistence, dispersion, ranking, scores, volatility, downside deviation, VaR/CVaR, Sharpe/Sortino/Calmar.
- **Guard Status:** like the other Phase 16 modules it is NOT in `PURE_MANIFEST`; it is clean under G1, G2, G4, and G5, and G3 flags only the required `fund_price_series` and `fund_return_series` imports (asserted directly in its test suite; G4/G5 also scan the whole private tree). No scanner exception was added.
