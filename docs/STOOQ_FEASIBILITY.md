# 🔍 Stooq Global Historical EOD — Production Access Feasibility Review

## 1. Executive Summary & Verdict

| Assessment Dimension | Status / Verdict | Basis |
| :--- | :--- | :--- |
| **Provider Candidate** | **Stooq** (`stooq.com`) | Evaluated as a potential multi-year historical EOD data provider. |
| **Audit Date** | August 27, 2026 | Phase 10B.1.5 evidence resolution. |
| **No-Key Unattended Access** | **`FAIL`** | Unattended HTTP requests receive a client-side JavaScript browser verification challenge rather than CSV data. |
| **API Key Contract** | **`UNVERIFIED`** | Third-party reports suggest an `apikey` parameter/flow may exist, but Sentinax has not independently verified the official contract. |
| **Key-Based Automation** | **`UNVERIFIED`** | Untested in automated backend without an official key contract. |
| **Price Adjustment Semantics** | **`UNVERIFIED`** | Conflicting reports on whether default historical series are raw or split/dividend-adjusted. |
| **Volume Adjustment Semantics**| **`UNKNOWN`** | Unspecified whether volume is adjusted alongside price. |
| **Candidate Classification** | **`YELLOW_CANDIDATE_BLOCKED`** | Feasible only if a legitimate, stable, machine-readable key contract is formally verified. |
| **Final Feasibility Verdict** | **`ACCESS_CONTRACT_UNVERIFIED` (CONDITIONAL_NO_GO)** | **Do NOT implement a Stooq adapter in current phase.** |

---

## 2. Verified Technical Evidence

### 2.1 Unauthenticated HTTP Client Access
- Direct HTTP GET requests to the historical download endpoint (e.g., `https://stooq.com/q/d/l/?s=aapl.us&i=d`) return `HTTP/1.1 200 OK` with `Content-Type: text/html` containing an OpenResty/client-side JavaScript Proof-of-Work (PoW) verification challenge.
- Unattended, zero-trust backend HTTP clients (e.g. `httpx`, `curl`) fail to receive CSV data.
- **Rule**: Sentinax will **not** programmatically solve or bypass browser challenges/CAPTCHAs.

### 2.2 Robots.txt Authority
- Observations of `robots.txt` were inconsistent across independent verification environments and are **not** used as the primary feasibility authority.

### 2.3 API Key & Official Contract State
- Sentinax has not verified an official self-service API key portal or usage agreement on Stooq.
- If legitimate manual key enrollment exists and provides machine-readable CSV downloads, it may be evaluated in a future setup verification step. Until then, `API_KEY_CONTRACT` remains `UNVERIFIED`.

### 2.4 Price & Corporate Action Adjustment Semantics
- Historical sources disagree on whether Stooq's default CSV stream reflects raw as-traded prices, split-adjusted prices, or total-return dividend-adjusted prices.
- **Policy**: The return engine and risk models must **not** ingest Stooq data until exact corporate action adjustment semantics are formally verified.

---

## 3. Conclusion & Next Steps

1. **Current Decision**: **NO ADAPTER IMPLEMENTATION**.
2. **Current State**: Phase 10A Alpha Vantage adapter remains the primary low-volume EOD ingestion channel.
3. **Future Path**: If a documented, legitimate Stooq key acquisition flow is established by the user, a secondary verification phase may probe key-based automated CSV retrieval.
