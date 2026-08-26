# Turkey Official Macroeconomic Data Layer

**Version:** 1.0  
**Effective Date:** 26 August 2026  
**Scope:** Macroeconomic data sources, PIT semantics, authentication, and canonical registry for Sentinax Private Engine.

---

## 1. Overview & Data Sources

| Source | Authority Level | Access Method | Automation | Freshness Basis | Secret Requirement | Tax Indexation Eligible? |
|---|---|---|---|---|---|---|
| **TCMB EVDS** | `TIER_1_REGULATORY` | REST API (JSON) | Automated | `EFFECTIVE_DATE` | `TCMB_EVDS_API_KEY` (Header `key`) | N/A (FX / Rates) |
| **TÜİK SDMX** | `TIER_1_REGULATORY` | SDMX 2.1 REST API | Automated | `PUBLISHED_AT` | None (Open Web Service) | **YES** (Yİ-ÜFE Only) |
| **ENAG Manual** | `TIER_3_AGGREGATOR` | Manual Ingestion | Manual (`VERIFIED`) | `PUBLISHED_AT` | None (Audit Trail) | **NO** (Strictly Prohibited) |

> [!IMPORTANT]
> **TÜİK SDMX Web Service: AVAILABLE SINCE JUNE 2026**  
> TÜİK's official SDMX 2.1 data portal web service is active. Automated machine-readable access uses official SDMX dataflows. Web scraping of legacy portal HTML is strictly prohibited.

---

## 2. Source Details & Specifications

### A. TCMB EVDS (Electronic Data Delivery System)
- **Base Endpoint:** `https://evds2.tcmb.gov.tr/service/evds/`
- **Security:** The user API key is provided strictly in the HTTP request header (`headers={"key": api_key}`).
  - *Invariant:* The API key is **NEVER** placed in URL query strings, application logs, cache keys, or exception messages.
- **Initial Core Series:**
  - `TR_FX_USDTRY` (`TP.DK.USD.A.YTL`): TCMB US Dollar Buying Rate (TL).
  - `TR_FX_EURTRY` (`TP.DK.EUR.A.YTL`): TCMB Euro Buying Rate (TL).
  - `TR_POLICY_RATE` (`TP.APIFON4`): TCMB 1-Week Repo Auction Rate / Policy Rate (Weighted Average).
- **PIT Semantics:** Observations represent EOD effective dates. Missing values remain `None` (never converted to 0.0).

### B. TÜİK SDMX (TurkStat)
- **Base Endpoint:** `https://data.tuik.gov.tr/api/sdmx/v1/data/`
- **Standard:** SDMX 2.1 JSON dataflow interface.
- **Initial Core Datasets:**
  - **TÜFE (CPI - 2003=100):**
    - `TR_CPI_TUIK_INDEX`: General CPI Index level.
    - `TR_CPI_TUIK_YOY`: Annual CPI % change.
    - `TR_CPI_TUIK_MOM`: Monthly CPI % change.
  - **Yİ-ÜFE (Domestic PPI - 2003=100):**
    - `TR_PPI_TUIK_INDEX`: General Domestic PPI Index level (*official statutory benchmark for tax indexation*).
    - `TR_PPI_TUIK_YOY`: Annual Domestic PPI % change.
    - `TR_PPI_TUIK_MOM`: Monthly Domestic PPI % change.
- **PIT Semantics:** `published_at` reflects official bulletin announcement timestamp; `effective_date` reflects the reference month. Revisions generate new records referencing `supersedes_record_id`.

### C. ENAG (Inflation Research Group)
- **Methodology:** Independent, research-based inflation estimates.
- **Lifecycle:** `PENDING` -> `VERIFIED` -> `REJECTED`.
  - Only records with `verification_status == VERIFIED` and a valid `source_url` can be used in decision support.
- **Strict Constraint:** ENAG data is **NEVER** used for statutory tax indexation or official accounting calculations.

---

## 3. Macro Identity Decoupling
Macroeconomic time-series have distinct identities from financial instruments (equities/funds). Macro series are defined in `macro_series` (Migration 006) and queried via `get_pit_macro_observation`, ensuring instrument identity purity.
