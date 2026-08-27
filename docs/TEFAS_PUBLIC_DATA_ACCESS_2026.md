# TEFAS 2026 Public Data Surface Empirical Verification & Access Contract Gate

## 1. Executive Verdict

- **Platform Operator:** Takasbank A.Ş. (İstanbul Takas ve Saklama Bankası A.Ş. / TEFAS Platformu).
- **Public Machine-Readable Access Status:** `GREEN_PUBLIC_API` (`OFFICIAL_OBSERVED`).
- **Production Gate Decision:** **`GO_PUBLIC_LOW_FREQUENCY`**.
- **Critical Finding:** Legacy ASP.NET endpoints (`/api/DB/BindHistoryInfo` and `/api/DB/BindHistoryAllocation`) are **REMOVED / DISABLED (HTTP 404)** in 2026. The 2026 Next.js architecture exposes clean, active, public JSON endpoints under `https://www.tefas.gov.tr/api/funds/*` that respond to ordinary HTTP `POST` requests without anti-bot circumvention or session cookies.
- **Target Recurring Market Data Cost:** **$0/month**.
- **Phase 11A.5 Code Status:** **NO PRODUCTION CODE WRITTEN** (Documentation & empirical verification only).

---

## 2. Empirical Probe Evidence Table (27 August 2026)

All probes executed using ordinary Python `urllib` / `curl` with transparent User-Agent (`Sentinax/1.0 (Personal Portfolio Engine)`), standard `Origin`/`Referer` headers, and zero anti-bot evasion libraries:

| Method | Target URL | Request Payload | HTTP Status | Response Content-Type | Response Bytes | JSON Parse? | Observed Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `GET` | `https://www.tefas.gov.tr/` | None | **200 OK** | `text/html; charset=utf-8` | 402,366 | N/A (HTML) | `OFFICIAL_OBSERVED` (Next.js SSR) |
| `GET` | `https://www.tefas.gov.tr/tr/fon-getirileri` | None | **200 OK** | `text/html; charset=utf-8` | 931,374 | N/A (HTML) | `OFFICIAL_OBSERVED` (Next.js Page) |
| `GET` | `https://www.tefas.gov.tr/tr/tarihsel-veriler` | None | **404 Not Found** | `text/html; charset=utf-8` | ~1,200 | N/A (HTML) | `OFFICIAL_OBSERVED` (Old route removed) |
| `POST` | `https://www.tefas.gov.tr/api/DB/BindHistoryInfo` | `fontip=YAT&fonkod=MAC&...` | **404 Not Found** | `application/json` | 185 | Yes (`ERR-006`) | `OFFICIAL_OBSERVED` (Disabled/Removed) |
| `POST` | `https://www.tefas.gov.tr/api/DB/BindHistoryAllocation` | `fonkod=MAC&...` | **404 Not Found** | `application/json` | 185 | Yes (`ERR-006`) | `OFFICIAL_OBSERVED` (Disabled/Removed) |
| `POST` | `https://www.tefas.gov.tr/api/funds/fonFiyatBilgiGetir` | `{"fonKodu":"MAC","dil":"TR","periyod":12}` | **200 OK** | `application/json;charset=UTF-8` | 47,649 | Yes (`resultList`: 252 rows) | `OFFICIAL_OBSERVED` (1Y Price History) |
| `POST` | `https://www.tefas.gov.tr/api/funds/fonFiyatBilgiGetir` | `{"fonKodu":"MAC","dil":"TR","periyod":60}` | **200 OK** | `application/json;charset=UTF-8` | 237,480 | Yes (`resultList`: 1257 rows) | `OFFICIAL_OBSERVED` (5Y Price History) |
| `POST` | `https://www.tefas.gov.tr/api/funds/fonBilgiGetir` | `{"fonKodu":"MAC","dil":"TR"}` | **200 OK** | `application/json;charset=UTF-8` | 368 | Yes (Current snapshot) | `OFFICIAL_OBSERVED` (Current NAV/AUM) |
| `POST` | `https://www.tefas.gov.tr/api/funds/fonUnvanAra` | `{"aranan":"","dil":"TR"}` | **200 OK** | `application/json;charset=UTF-8` | 240,321 | Yes (`resultList`: 2589 funds) | `OFFICIAL_OBSERVED` (Fund Universe) |

---

## 3. Discovered 2026 Public API Surface (`/api/funds/*`)

### A) Historical Price Series: `fonFiyatBilgiGetir`
- **Endpoint:** `POST https://www.tefas.gov.tr/api/funds/fonFiyatBilgiGetir`
- **Payload:** `{"fonKodu": "MAC", "dil": "TR", "periyod": 60}`
- **Supported `periyod` (Months):** `1` (1M), `3` (3M), `6` (6M), `12` (1Y), `36` (3Y), `60` (5Y). (`periyod > 60` returns system error).
- **Top-Level JSON Structure:**
  ```json
  {
    "errorCode": null,
    "errorMessage": null,
    "resultList": [
      {
        "fonKodu": "MAC",
        "fonUnvan": "MARMARA CAPITAL PORTFÖY HİSSE SENEDİ (TL) FONU (HİSSE SENEDİ YOĞUN FON)",
        "kategoriDerece": 150,
        "kategoriFonSay": 199,
        "tarih": "2021-08-27",
        "fiyat": 0.058211
      }
    ]
  }
  ```
- **Historical Fields Available:** `fonKodu`, `fonUnvan`, `tarih` (`YYYY-MM-DD`), `fiyat` (numeric unit NAV price).
- **Fields NOT in History Surface:** `PORTFOYBUYUKLUK` (AUM), `KISISAYISI` (Investors), `TEDPAYSAYISI` (Shares) are **not present** in `fonFiyatBilgiGetir`.

### B) Current Valuation & AUM Snapshot: `fonBilgiGetir`
- **Endpoint:** `POST https://www.tefas.gov.tr/api/funds/fonBilgiGetir`
- **Payload:** `{"fonKodu": "MAC", "dil": "TR"}`
- **Response Fields:**
  - `fonKodu`: 3-letter provider alias.
  - `sonFiyat`: Latest unit NAV price (e.g. `0.76165`).
  - `gunlukGetiri`: Daily return percentage (e.g. `0.8381`).
  - `payAdet`: Outstanding shares / units (`5725524142`).
  - `portBuyukluk`: Total portfolio size / AUM in TRY (`4360844111.72`).
  - `fonKategori`: Category string (e.g. `"Hisse Senedi Fonu"`).
  - `yatirimciSayi`: Total investor count (`36070`).
  - `pazarPayi`: Category market share (`1.64`).

### C) Fund Universe Enumeration: `fonUnvanAra`
- **Endpoint:** `POST https://www.tefas.gov.tr/api/funds/fonUnvanAra`
- **Payload:** `{"aranan": "", "dil": "TR"}`
- **Response:** Array of **2,589 active funds** with `fonKodu` and `fonUnvan`.

---

## 4. Fields & Capabilities Matrix

| Field | Turkish Provider Key | 2026 Official Surface | Machine-Readable? | Historical Depth | PIT-Safe? | Needed by Sentinax? | Status & Evidence |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Fund Code | `fonKodu` | `fonFiyatBilgiGetir` / `fonBilgiGetir` | Yes | 5Y | Yes (Economic Alias) | **Yes** | `OFFICIAL_OBSERVED` |
| Fund Title | `fonUnvan` | `fonFiyatBilgiGetir` / `fonBilgiGetir` | Yes | Current only | **No (Look-ahead)** | **Yes** | `CURRENT_METADATA_ONLY` |
| Trade Date | `tarih` | `fonFiyatBilgiGetir` | Yes | 5Y | Yes (`YYYY-MM-DD`) | **Yes** | `OFFICIAL_OBSERVED` |
| Unit Price | `fiyat` / `sonFiyat` | `fonFiyatBilgiGetir` / `fonBilgiGetir` | Yes | 5Y | Yes (Clean decimal) | **Yes** | `OFFICIAL_OBSERVED` |
| Portfolio Size (AUM) | `portBuyukluk` | `fonBilgiGetir` | Yes | Current only | Yes (for current) | Optional | `CURRENT_ONLY_OBSERVED` |
| Investor Count | `yatirimciSayi` | `fonBilgiGetir` | Yes | Current only | Yes (for current) | Optional | `CURRENT_ONLY_OBSERVED` |
| Shares Outstanding | `payAdet` | `fonBilgiGetir` | Yes | Current only | Yes (for current) | Optional | `CURRENT_ONLY_OBSERVED` |
| Fund Category | `fonKategori` | `fonBilgiGetir` | Yes | Current only | **No (Look-ahead)** | Optional | `CURRENT_METADATA_ONLY` |
| ISIN | N/A | Excluded from public endpoints | No | N/A | N/A | Optional | `UNAVAILABLE_ON_PUBLIC_TEFAS` |
| Currency | N/A | Implied TRY | No (Implicit TRY) | N/A | Verify via Master | **Yes** | `METADATA_AUTHORITY_REQUIRED` |
| Portfolio Allocation | N/A | `dagilimSiraliGetirT` (Complex format) | Partial | Unverified | Unverified | Future Phase | `UNVERIFIED_STRUCTURE` |
| Management Fee | N/A | Excluded from EOD API | No in TEFAS | N/A | N/A | Optional | `KAP_AUTHORITY_REQUIRED` |

---

## 5. Historical Depth & Range Constraints

- **Historical Depth Limit:** `periyod=60` provides **5 Years (60 Months)** of continuous daily prices per fund (`OFFICIAL_OBSERVED`).
- **Platform Inception (>5Y):** Deprecated claim of `>=10Y` directly via standard public API is refuted; calls with `periyod > 60` fail with system error. Deeper historical bootstrap requires Phase 13 archive imports.
- **Bulk vs Single Fund Semantics:**
  - `fonFiyatBilgiGetir` requires `fonKodu` (single fund per request).
  - `fonUnvanAra` provides the master list of 2,589 fund codes.
  - Daily incremental synchronization requires looping over the active portfolio fund universe.

---

## 6. Point-in-Time (PIT) & Identity Semantics

1. **Provider Identity Authority:** `FONKODU` (3-letter alias) maps to canonical `instrument_id` (UUID) via Sentinax's `InstrumentResolverService`.
2. **Metadata Look-Ahead Limitation (`CURRENT_METADATA_ONLY`):**
   - Historical rows in `fonFiyatBilgiGetir` return the *current* `fonUnvan`.
   - Historical name/category changes are not preserved in the time-series response.
3. **Price Lineage:**
   - `trade_date`: Parsed from `tarih` (`YYYY-MM-DD`).
   - `close`: Parsed from `fiyat` (strict `Decimal`).
   - `retrieved_at`: UTC timezone-aware ingestion timestamp.
   - `published_at`: `None` (no microsecond publication timestamp exposed).
   - `mode == SOURCE_AS_OF`: Returns `UNAVAILABLE_SOURCE_AS_OF`.

---

## 7. Architecture, Bot Protection & Automation Terms

- **Architecture:** Next.js frontend with RESTful backend endpoints (`OFFICIAL_OBSERVED`).
- **Bot Protection & WAF:** Ordinary HTTP `POST` requests with standard headers (`Content-Type: application/json`, `Origin`, `Referer`) succeed reliably without cookies, CAPTCHAs, or browser emulation.
- **Anti-Bot Circumvention Ban:** Sentinax strictly rejects `curl_cffi`, TLS fingerprint spoofing, and stealth browser automation. Standard `httpx` with 500ms-1000ms polite rate-limiting is completely sufficient.
- **Takasbank Member Web Services:** Institutional participant web services (requiring Takas Menü credentials) are confirmed **`OUT_OF_SCOPE_FOR_SENTINAX`**.
- **Private Automation Terms:** Public disclosure portal operated under Capital Markets Board (SPK) transparency regulations; private non-commercial low-frequency reading is `NOT_PROHIBITED_BUT_UNDOCUMENTED`.

---

## 8. Fallback Sources

1. **KAP (Kamuyu Aydınlatma Platformu):** Authoritative source for fund prospectuses, founding notices, management fee schedules, and material event disclosures (`OFFICIAL_DOCUMENTED`).
2. **Excel Public Downloads:** Previous route `/tr/tarihsel-veriler` is `404 Not Found` (`OFFICIAL_OBSERVED`). Downstream archive imports deferred to Phase 13.

---

## 9. SourceTier Classification

- **Recommended SourceTier:** `SourceTier.TIER_2_EXCHANGE` (Takasbank operates central clearing/settlement infrastructure and the official TEFAS fund distribution platform under SPK authority).
- **Provider Access Status:** `GREEN_PUBLIC_API` (Direct, unattended JSON access verified).

---

## 10. Phase 11B Implementation Scope

### Decision: **`GO_PUBLIC_LOW_FREQUENCY`**

**Phase 11B Implementation Plan:**
1. **Module:** `backend/engine/private/providers/tefas_eod.py`.
2. **Endpoints Used:**
   - `https://www.tefas.gov.tr/api/funds/fonFiyatBilgiGetir` (Historical and rolling 5-year daily prices).
   - `https://www.tefas.gov.tr/api/funds/fonBilgiGetir` (Current valuation, AUM, investor count, outstanding units).
   - `https://www.tefas.gov.tr/api/funds/fonUnvanAra` (Universe enumeration).
3. **Invariants:**
   - Zero anti-bot evasion dependencies (pure Python standard library / existing `httpx`).
   - Strict `Decimal` parsing for prices and totals (no floats).
   - Snapshot immutability with SHA-256 `payload_hash` and UTC timezone-aware `retrieved_at`.
   - Dual identity resolution via `InstrumentResolverService`.
