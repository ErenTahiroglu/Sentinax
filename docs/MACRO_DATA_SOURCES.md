# Turkey Official Macroeconomic Data Layer

**Version:** 1.1 (Hardened)  
**Effective Date:** 26 August 2026  
**Scope:** Macroeconomic data sources, PIT semantics, authentication, contract verification, and canonical registry for Sentinax Private Engine.

---

## 1. Overview & Data Sources

| Source | Authority Level | Access Method | Contract Status | Freshness Basis | Secret Requirement | Tax Indexation Eligible? |
|---|---|---|---|---|---|---|
| **TCMB EVDS** | `TIER_1_REGULATORY` | REST API (JSON) | **VERIFIED (EVDS2)** | `EFFECTIVE_DATE` | `TCMB_EVDS_API_KEY` (Header `key`) | N/A (FX / Funding Rates) |
| **TÜİK SDMX** | `TIER_1_REGULATORY` | SDMX 2.1 REST API | **UNVERIFIED (YELLOW)** | `PUBLISHED_AT` | None (Open Web Service) | **YES** (Yİ-ÜFE Only, once verified) |
| **ENAG Manual** | `TIER_3_AGGREGATOR` | Manual Ingestion | **VERIFIED (MANUAL)** | `PUBLISHED_AT` | None (Audit Trail) | **NO** (Strictly Prohibited) |

---

## 2. Source Details & Specifications

### A. TCMB EVDS (Electronic Data Delivery System)
- **Base Endpoint:** `https://evds2.tcmb.gov.tr/service/evds/`
- **EVDS3 Beta Transition:** TCMB launched EVDS3 Beta on **26 January 2026**. EVDS2 remains fully supported and accessible concurrently during the transition.
- **Security:** The user API key is provided strictly in the HTTP request header (`headers={"key": api_key}`).
  - *Invariant:* The API key is **NEVER** placed in URL query strings, application logs, cache keys, or exception messages.
- **Verified Core Series:**
  - `TR_FX_USDTRY` (`TP.DK.USD.A.YTL`): TCMB Gösterge Niteliğindeki ABD Doları Döviz Alış Kuru (TL).
  - `TR_FX_EURTRY` (`TP.DK.EUR.A.YTL`): TCMB Gösterge Niteliğindeki Euro Döviz Alış Kuru (TL).
  - `TR_TCMB_AOFM` (`TP.APIFON4`): TCMB Ağırlıklı Ortalama Fonlama Maliyeti (AOFM) (%).
- **Policy Rate Status:**
  - *Note:* `TP.APIFON4` represents the **Weighted Average Cost of Funding (AOFM)**, not the 1-week repo policy rate. The statutory 1-week repo policy rate code in EVDS is marked `UNVERIFIED` and disabled until confirmed by official EVDS series documentation.

### B. TÜİK SDMX (TurkStat)
- **Status:** **YELLOW / UNVERIFIED DATAFLOWS**
- **Base Endpoint:** `https://data.tuik.gov.tr/api/sdmx/v1/`
- **Audit Findings:** While TÜİK's data portal operates on SDMX 2.1 standards, exact public machine-readable dataflow codes and codelist dimensions require official catalog discovery confirmation. Hardcoded guesses (e.g. `CPI_INDEX_2003`) are disabled (`is_active = False`) until verified against the live portal metadata.

### C. ENAG (Inflation Research Group)
- **Methodology:** Independent, research-based inflation estimates.
- **Lifecycle & History:** `PENDING` -> `VERIFIED` -> `REJECTED`.
  - Only records with `verification_status == VERIFIED` and a valid `source_url` can be used in decision support.
  - Overwriting existing records is strictly forbidden; updates must reference `supersedes_record_id`.
  - Verification is immutable regarding substantive data (values, dates, sources).
- **Strict Constraint:** ENAG data is **NEVER** used for statutory tax indexation or official accounting calculations.

---

## 3. Point-in-Time (PIT) Storage & Immutability
Macroeconomic time-series are stored in `macro_series` and `macro_observations` (Migration 006):
- Full-row immutability enforced by PostgreSQL trigger (allow-list: only `is_superseded` and `superseded_at` can be updated).
- Insertion of a revision automatically supersedes the previous observation.
- Point-in-Time RPC `get_pit_macro_observation` prevents historical lookahead leaks.
