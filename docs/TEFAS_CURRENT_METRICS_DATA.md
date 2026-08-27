# TEFAS Current Fund Valuation & Metrics Ingestion Specification (Phase 11D.2)

## 1. Executive Overview

This document specifies the architecture, data models, temporal contracts, and fail-closed security invariants for ingesting **Current Fund Valuation and Size Metrics** from TEFAS (Takasbank Turkey Electronic Fund Distribution Platform).

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                    TEFAS PUBLIC CURRENT METRICS PIPELINE                       │
├───────────────────────┬────────────────────────┬───────────────────────────────┤
│    TEFAS Endpoint     │   Normalized Storage   │      Temporal Semantics       │
│  /api/funds/          │  TefasFundCurrent      │  - effective_date = None      │
│  fonBilgiGetir        │  MetricsObservation    │  - published_at = None        │
│                       │                        │  - observed_at = retrieved_at │
└───────────────────────┴────────────────────────┴───────────────────────────────┘
```

---

## 2. Official Endpoint & Protocol Contract

- **Endpoint:** `POST https://www.tefas.gov.tr/api/funds/fonBilgiGetir`
- **Access Classification:**
  - **Provider Access Status:** `ProviderAccessStatus.YELLOW` (Public undocumented web endpoint; zero SLA).
  - **Source Quality:** `SourceTier.TIER_2_EXCHANGE` (Takasbank official central clearing & settlement entity).
  - **Official Source:** `True`
  - **Developer API / SLA:** `False`
- **Request Body:**
  ```json
  {
    "fonKodu": "<TEFAS_CODE>",
    "dil": "TR"
  }
  ```
- **Transparent Headers Only:**
  - `Content-Type: application/json`
  - `Accept: application/json`
  - `User-Agent: Sentinax/1.0 (Personal Portfolio Engine)`
  - `Origin: https://www.tefas.gov.tr`
  - `Referer: https://www.tefas.gov.tr/TarihselVeriler.aspx`

---

## 3. Strict Temporal Semantics & Identity Lookup Date

### A) Source Economic Date Absence (`UNKNOWN`)
The response from `fonBilgiGetir` contains **no publication timestamp, valuation date, or effective economic date**.

- `retrieved_at`: Precise timezone-aware UTC timestamp of network ingestion.
- `published_at`: Strictly `None` (no timestamp fabrication).
- `effective_date`: Strictly `None` (no fabrication from `retrieved_at.date()` or `date.today()`).
- `SOURCE_AS_OF` resolution mode: Constant `UNAVAILABLE_SOURCE_AS_OF`.

### B) Identity Lookup Date Distinction
Identity resolution (looking up `ProviderAliasRecord` or `InstrumentRecord`) requires an as-of reference date.
- **Reference Date for Alias Lookup:** `retrieved_at.date()` (UTC).
- **Rule:** This reference date is used **strictly for identity resolution** and is never mapped to `effective_date`.

---

## 4. Capability Boundary & Multi-Pay-Group Fail-Closed Safety

### A) TRY-Only Capability Scope
`PUBLIC_TEFAS_METRICS_SUPPORTED_CURRENCY = Currency.TRY ONLY`

Across the verified sample, the public current endpoint returned a TRY-reference reporting basis. Sentinax intentionally limits this adapter to canonical TRY instruments until share-class discrimination exists.

### B) Non-TRY Preflight Rejection
If the resolved canonical instrument has `currency != Currency.TRY` (e.g. `USD`, `EUR`):
- `DataStatus.UNAVAILABLE` is returned **before HTTP dispatch**.
- `AMBIGUOUS_PAY_GROUP_CURRENCY` warning is recorded.
- Zero network calls are dispatched.

---

## 5. Field Semantics & Decision Relevance

| Source Field | Normalized Property | Python Type | Semantic Meaning | Status Control | Decision Relevance |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `portBuyukluk` | `portfolio_size` | `Decimal` | Net Asset Value / Fund Size (TRY) | **Mandatory** (`is_valid`) | High (AUM / scale risk) |
| `payAdet` | `outstanding_units` | `Decimal` | Outstanding Participation Units | Secondary (`COMPLETE`) | Low / Diagnostic |
| `yatirimciSayi` | `investor_count` | `int` | Registered Unit-Holder Count | Secondary (`COMPLETE`) | Medium (Breadth context) |
| `sonFiyat` | `reported_current_unit_price` | `Decimal` | Latest Reported Unit NAV Price | **Diagnostic Only** | Low (Cross-check only) |
| `fonKategori` | — | — | TEFAS High-Level Category String | Raw Snapshot Only | Low (Metadata label) |
| `gunlukGetiri` | — | — | Daily Return % (Provider-calculated) | Raw Snapshot Only | None (Sentinax calculates returns) |
| `pazarPayi` | — | — | Category Market Share % | Raw Snapshot Only | Low (Informational) |

---

## 6. Aggregate Status Contract

- **`COMPLETE`:**
  - `portfolio_size` is valid (finite `Decimal >= 0`, `currency == TRY`).
  - `outstanding_units` is valid (finite `Decimal >= 0`).
  - `investor_count` is valid (non-negative `int >= 0`).
- **`PARTIAL`:**
  - `portfolio_size` is valid (finite `Decimal >= 0`, `currency == TRY`), but `outstanding_units` or `investor_count` is missing/invalid.
- **`UNAVAILABLE`:**
  - `portfolio_size` is missing, negative, non-finite, or `currency != TRY`.
  - Or identity mismatch / resolution failure / HTTP failure / error envelope.

---

## 7. Diagnostic Accounting Reconciliation

When `sonFiyat`, `payAdet`, and `portBuyukluk` are all valid:
$$\text{calc\_aum} = \text{reported\_current\_unit\_price} \times \text{outstanding\_units}$$
$$\text{abs\_diff} = |\text{calc\_aum} - \text{portfolio\_size}|$$
$$\text{rel\_diff} = \frac{\text{abs\_diff}}{\text{portfolio\_size}}$$

These differences are recorded in `TefasFundMetricsSnapshot.reconciliation_absolute_diff` and `reconciliation_relative_diff` for diagnostic integrity audits.

---

## 8. Immutable System History & Future PIT Resolution

- Every current metrics fetch creates an immutable `TefasFundMetricsSnapshot` and `NormalizedObservationRecord`.
- Future point-in-time metrics resolution will operate in **`SYSTEM_AS_OF`** mode using immutable `retrieved_at` snapshot lineages.
- Historical backfilling is strictly prohibited.
