# TEFAS 2026 Public Data Surface Re-Discovery & Production Access Contract Gate

## 1. Executive Verdict

- **Platform Authority:** `TIER_1` (Official Takasbank Fund Allocation Platform / Takasbank A.Ş.).
- **Public Machine-Readable Access Status:** `YELLOW_CANDIDATE` (Public endpoints exist but lack documented SLA; subject to WAF / rate limiting).
- **Production Gate Verdict:** `CONDITIONAL_GO_LOW_FREQUENCY` (Unattended low-frequency daily batch requests to `/api/DB/BindHistoryInfo` or Next.js `/api/funds/*` are viable for standard HTTP clients under strict non-evasion rules; bot-evasion tools like `curl_cffi` TLS spoofing are strictly rejected).
- **Target Recurring Market Data Cost:** **$0/month**.
- **Production Code Status in Phase 11A:** **NO CODE WRITTEN** (Discovery & feasibility gate only).

---

## 2. 2026 Public-Site Architecture

- **Architecture Classification:** `HYBRID_NEXTJS_AND_ASPNET` (`OFFICIAL_OBSERVED`).
- **Frontend Stack:** Modernized Next.js frontend routes (e.g., `/tr/fon-getirileri`, `/tr/tarihsel-veriler`) backed by server-rendered pages and legacy/v2 JSON endpoints.
- **WAF / Interstitial Layer:** Protected by edge security (Akamai / Cloudflare edge network). High-frequency scrapers or headless browsers without standard HTTP headers receive 403 Forbidden or challenge pages (`OFFICIAL_OBSERVED` / `THIRD_PARTY_DISCOVERY`).
- **Anti-Bot Circumvention Policy:** Sentinax explicitly bans browser fingerprint impersonation (`curl_cffi`), CAPTCHA solvers, and stealth drivers. Standard unattended `httpx`/`requests` with polite intervals must govern all interactions.

---

## 3. Old Endpoint Status

| Endpoint | Method | Observed Status | Content Type | Evidence Label |
| :--- | :--- | :--- | :--- | :--- |
| `https://www.tefas.gov.tr/api/DB/BindHistoryInfo` | `POST` | `FUNCTIONAL_PUBLIC` (Requires POST form data with `fontipi`, `bastarih`, `bittarih`, `fonkod`) | `application/json` | `OFFICIAL_OBSERVED` |
| `https://www.tefas.gov.tr/api/DB/BindHistoryAllocation` | `POST` | `FUNCTIONAL_PUBLIC` (Returns asset distribution percentages for fund code over date range) | `application/json` | `OFFICIAL_OBSERVED` |
| `https://www.tefas.gov.tr/api/DB/BindFundInfo` | `POST` | `FUNCTIONAL_PUBLIC` (Returns general fund summary / metadata) | `application/json` | `OFFICIAL_OBSERVED` |
| `https://www.tefas.gov.tr/api/DB/BindComparisonFundAllocation` | `POST` | `FUNCTIONAL_PUBLIC` (Comparative allocation matrix) | `application/json` | `OFFICIAL_OBSERVED` |

*Verdict:* Legacy ASP.NET backend endpoints (`/api/DB/BindHistoryInfo`) remain active and machine-readable in 2026 when queried via standard POST requests with `DD.MM.YYYY` date formatting.

---

## 4. Current Public Machine-Readable Surfaces

1. **Daily & Historical Price Endpoint (`BindHistoryInfo`):**
   - **URL:** `https://www.tefas.gov.tr/api/DB/BindHistoryInfo`
   - **Method:** `POST`
   - **Payload Format:** `form-urlencoded` or JSON body with keys:
     - `fontipi`: Fund group filter (e.g., `YAT` for Securities/Yatırım Fonları, `EMK` for Pension/Emeklilik).
     - `bastarih`: Start date in `DD.MM.YYYY` format.
     - `bittarih`: End date in `DD.MM.YYYY` format.
     - `fonkod`: Optional 3-letter TEFAS fund code (e.g., `TCD`, `NNF`, `TI1`). If empty, returns bulk data for all funds in date range.
   - **Response Structure:** `{"draw": 0, "recordsTotal": N, "recordsFiltered": N, "data": [{...}]}`.
2. **Asset Allocation Endpoint (`BindHistoryAllocation`):**
   - **URL:** `https://www.tefas.gov.tr/api/DB/BindHistoryAllocation`
   - **Method:** `POST`
   - **Payload Format:** `{"fonkod": "TCD", "bastarih": "01.01.2026", "bittarih": "27.08.2026"}`.
   - **Response:** Daily asset weights (Stocks, Eurobonds, Reverse Repo, Precious Metals, etc.).

---

## 5. Fields Matrix

| Field | Turkish Provider Key | Official Surface | Machine-Readable? | Historical Depth | PIT-Safe? | Needed by Sentinax? | Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Fund Code | `FONKODU` | `BindHistoryInfo` | Yes | >=10Y | Yes (Economic Alias) | **Yes** | `OFFICIAL_VERIFIED` |
| Fund Title | `FONUNVAN` | `BindHistoryInfo` | Yes | Current only | **No (Look-ahead)** | **Yes** | `CURRENT_METADATA_ONLY` |
| Price Date | `TARIH` | `BindHistoryInfo` | Yes | >=10Y | Yes (Unix ms / `DD.MM.YYYY`) | **Yes** | `OFFICIAL_VERIFIED` |
| Unit NAV Price | `FIYAT` | `BindHistoryInfo` | Yes | >=10Y | Yes (Clean decimal) | **Yes** | `OFFICIAL_VERIFIED` |
| Portfolio Size | `PORTFOYBUYUKLUK` | `BindHistoryInfo` | Yes | >=10Y | Yes (TRY total NAV) | **Yes** | `OFFICIAL_VERIFIED` |
| Investor Count | `KISISAYISI` | `BindHistoryInfo` | Yes | >=10Y | Yes (Integer) | **Yes** | `OFFICIAL_VERIFIED` |
| Shares Outstanding | `TEDPAYSAYISI` | `BindHistoryInfo` | Yes | >=10Y | Yes (Total units) | **Yes** | `OFFICIAL_VERIFIED` |
| Fund Category | `FONKATEGORI` / `FONUNVANTIP` | `BindFundInfo` | Yes | Current only | **No (Look-ahead)** | Optional | `CURRENT_METADATA_ONLY` |
| ISIN | N/A | Excluded from `BindHistoryInfo` | No (KAP required) | N/A | N/A | Optional | `UNAVAILABLE_ON_PUBLIC_TEFAS_EOD` |
| Currency | N/A | Implied TRY / FX in title | No (Implicit TRY) | N/A | Verify via Master | **Yes** | `METADATA_AUTHORITY_REQUIRED` |
| Asset Allocation | `HISSE`, `DEVLET_TAHVILI`, etc. | `BindHistoryAllocation` | Yes | >=5Y | Yes (Historical point) | Future Phase | `OFFICIAL_VERIFIED` |
| Management Fee | `YONETIM_UCRETI` | KAP / Fund Prospectus | No in EOD API | Current only | No in EOD | Optional | `KAP_AUTHORITY_REQUIRED` |

---

## 6. Historical Depth & Range Semantics

- **Historical Availability:** Daily prices are accessible back to platform inception (2015+) and fund inception dates (`OFFICIAL_VERIFIED`).
- **Request Chunking Rule:** Large date queries (>90 days in bulk or >1 year for single fund) can timeout or be rate-limited by upstream web tier. Adapters must chunk historical fetches into 90-day batches (`THIRD_PARTY_DISCOVERY` / `OFFICIAL_OBSERVED`).
- **Bulk vs Single Fund Semantics:**
  - Omitting `fonkod` returns all active funds for the given date range (useful for daily incremental synchronization).
  - Specifying `fonkod` returns time-series for a single fund (useful for backfilling historical depth).

---

## 7. Identity & ISIN

- **TEFAS Provider Alias:** 3-letter alphanumeric code (e.g. `TCD`, `MAC`, `TI1`, `YAS`).
- **ISIN Resolution:** Public TEFAS `BindHistoryInfo` does not supply ISIN codes. ISIN mappings must be sourced via KAP (Kamuyu Aydınlatma Platformu) or maintained in Sentinax's `InstrumentResolverService`.
- **Identity Invariant:** `GlobalEODObservation` and domain models must resolve the 3-letter alias to a canonical `instrument_id` (UUID) in the Instrument Master.

---

## 8. Fund Taxonomy & Domain Alignment

- **Current `domain.py` Limitation:** `InstrumentType` currently contains only 4 TEFAS types (`TEFAS_MONEY_MARKET`, `TEFAS_EQUITY`, `TEFAS_VARIABLE`, `TEFAS_BALANCED`).
- **Actual TEFAS Public Universe:** Contains ~15+ distinct SPK fund categories:
  - *Borçlanma Araçları* (Fixed Income / Debt Instruments)
  - *Hisse Senedi* (Equity)
  - *Değişken* (Variable)
  - *Fon Sepeti* (Fund of Funds)
  - *Kıymetli Madenler* (Precious Metals / Gold)
  - *Para Piyasası* (Money Market)
  - *Karma* (Mixed / Balanced)
  - *Katılım* (Islamic / Sharia-compliant)
  - *Serbest* (Hedge / Qualified Investor Funds)
  - *Gayrimenkul Yatırım Fonları* (REIT Funds / GYF)
  - *Girişim Sermayesi* (Venture Capital / GSYF)
- **Recommended Strategy (Strategy A):**
  Maintain canonical `AssetClass.FUND` with a general `TEFAS_MUTUAL_FUND` / expanded canonical types, and store fine-grained SPK category as structured metadata attributes rather than bloating the top-level Python enum with 30 transient classifications.

---

## 9. Point-in-Time (PIT) Limitations & Disclaimer

- **Official TEFAS Historical Metadata Warning (`OFFICIAL_VERIFIED`):**
  Takasbank explicitly notes that historical fund performance queries display the *current* fund management company, current fund title, and current category assignment. If a fund changed its strategy or portfolio manager in 2022, a query for 2020 will reflect the 2026 title and category.
- **Sentinax PIT Rules:**
  - `price_date`: Authoritative economic date.
  - `unit_price`: Authoritative economic historical price.
  - `fund_title` / `category`: Classified as `CURRENT_METADATA_ONLY`. Historical backtests must NOT infer past asset eligibility from current fund categories without effective-dated master history.
  - `retrieved_at`: Required timezone-aware local ingestion timestamp.
  - `published_at`: `None` (TEFAS does not expose immutable publication micro-timestamps).

---

## 10. Bot Protection, Rate Limits & Terms of Service

- **Bot Protection:** Standard rate-limiting and WAF headers are active. Rapid-fire unthrottled requests trigger HTTP 403 / 429.
- **Anti-Bot Non-Circumvention Rule:** Sentinax will NEVER use `curl_cffi`, TLS signature spoofing, or headless stealth browsers. Standard async `httpx` with `User-Agent: Sentinax/1.0 (Personal Portfolio Engine)` and minimum 500ms request delays will be used.
- **SLA & Rate Limits:** Undocumented by Takasbank (`PUBLIC_RATE_LIMIT = UNDOCUMENTED`).
- **Terms & Private Automation:** Platform is a public financial disclosure portal operated pursuant to Capital Markets Board (SPK) regulations. Non-commercial, low-frequency automated reading for personal portfolio risk analysis is not prohibited (`PRIVATE_AUTOMATION = NOT_PROHIBITED_BUT_UNDOCUMENTED`).

---

## 11. Takasbank Member Web Services Exclusion

- **Takasbank Institutional Web Services:** Takasbank operates formal SOAP/REST web services for clearing members (banks, brokerage houses, custody participants) requiring Takas Menü credentials and VPN/leased lines.
- **Sentinax Scope:** `MEMBER_WEB_SERVICES = OUT_OF_SCOPE_FOR_SENTINAX`. Sentinax is an independent personal architecture and relies exclusively on public disclosures and official open endpoints.

---

## 12. Zero-Cost Official Fallbacks

1. **KAP (Kamuyu Aydınlatma Platformu - `kap.org.tr`):**
   - Official daily fund price bulletins and portfolio composition disclosures published daily.
   - Machine-readable public JSON endpoints (`kap.org.tr/tr/api/...`) available for public filings.
2. **Periodic Excel Exports:**
   - TEFAS web interface provides daily and monthly historical Excel (`.xlsx`) downloads via `https://www.tefas.gov.tr/tr/tarihsel-veriler`.
   - Suitable for Phase 13 manual/bootstrap ingestion if real-time web transport is ever interrupted.

---

## 13. Access Matrix

| Surface | Official? | Public? | Auth Required? | Ordinary HTTP Works? | Bot Protection? | Machine-Readable? | Documented SLA? | Production Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `TEFAS Web UI (/tr/...)` | Yes | Yes | No | Yes (HTML) | Medium (WAF) | No (HTML) | No | `MANUAL_VERIFICATION_ONLY` |
| `TEFAS BindHistoryInfo` | Yes | Yes | No | **Yes (POST)** | Low/Medium | **Yes (JSON)** | No | `APPROVED_FOR_PHASE_11B` |
| `TEFAS BindHistoryAllocation`| Yes | Yes | No | **Yes (POST)** | Low/Medium | **Yes (JSON)** | No | `APPROVED_FOR_ALLOCATION` |
| `Takasbank Member Web Services` | Yes | No | **Yes (Member Login)** | No | High | Yes | Yes | `OUT_OF_SCOPE` |
| `KAP Fund Bulletins` | Yes | Yes | No | **Yes (GET/POST)** | Low | **Yes (JSON/PDF)** | No | `APPROVED_ZERO_COST_FALLBACK` |

---

## 14. Phase 11B Implementation Gate & Scope

### Implementation Gate: `CONDITIONAL_GO_LOW_FREQUENCY`

**Conditions for Phase 11B Provider Adapter:**
1. **Low Frequency Batch Ingestion:** Adapter must execute once daily after market close (~19:00 - 21:00 TRT) with minimum 1.0-second jittered inter-request delays.
2. **Chunking Contract:** Historical backfills must be chunked into maximum 90-day intervals to prevent upstream gateway timeouts.
3. **Fail-Closed Validation:** All decimal parsing must strictly use `Decimal` (no floats). Missing or malformed values must never default to zero.
4. **Snapshot Immutability:** Raw JSON responses must be hashed with SHA-256 (`payload_hash`) and stored with UTC timezone-aware `retrieved_at` timestamps.
5. **No Evasion Packages:** Zero external anti-bot bypass dependencies. Pure Python standard library or existing `httpx` dependencies only.
