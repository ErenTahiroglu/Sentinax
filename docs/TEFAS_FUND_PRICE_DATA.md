# TEFAS 2026 Fund Price History Core (Phase 11B)

## 1. Overview & Architectural Scope

The **TEFAS 2026 Fund Price History Adapter** (`TefasFundPriceProvider`) integrates daily net asset value (NAV) / unit price series for Turkish mutual funds and pension funds from Takasbank's official Fund Information Platform (TEFAS).

- **Provider Name**: `TEFAS`
- **Provider Version**: `2026.1.0`
- **Data Cost**: **$0/month** (Zero recurring license or API subscription cost)
- **Source Tier**: `SourceTier.TIER_2_EXCHANGE` (Takasbank official central clearing & settlement platform)
- **Access Classification**: `ProviderAccessStatus.YELLOW` (Official public JSON web surface; no developer SLA, quota endpoint, or API key)
- **Target Endpoint**: `POST https://www.tefas.gov.tr/api/funds/fonFiyatBilgiGetir`
- **Supported History Window**: Up to **5 Years** (`periyod=60`)

---

## 2. Public API Surface & Request Contract

### 2.1 Endpoint Specification

```http
POST /api/funds/fonFiyatBilgiGetir HTTP/1.1
Host: www.tefas.gov.tr
Accept: application/json
Content-Type: application/json
Origin: https://www.tefas.gov.tr
Referer: https://www.tefas.gov.tr/tr/fon-getirileri
User-Agent: Sentinax/1.0

{
  "fonKodu": "MAC",
  "dil": "TR",
  "periyod": 60
}
```

### 2.2 Period Constraints

TEFAS strictly enforces valid values for `periyod` representing duration in months:
- **Supported Periods**: `{1, 3, 6, 12, 36, 60}`
- **Maximum Public Depth**: 60 months (5 years)
- Any unsupported period (e.g. `2`, `24`, `61`, `120`, `"5Y"`, negative or null) is rejected **before HTTP dispatch** with `INVALID_PERIOD`.

---

## 3. Data Integrity & Decimal Precision Rules

1. **Exact Lexical Decimal Parsing**:
   - Upstream JSON numbers are parsed directly from text using Python's `json.loads(..., parse_float=Decimal)` lexical boundary hook.
   - Binary floating-point representation (`float`) is strictly prohibited.
2. **Unit Price Boundary**:
   - `unit_price` must be finite, non-null, and strictly positive (`unit_price > Decimal("0")`).
   - Zero, negative, `NaN`, and `Infinity` are rejected as `INVALID_OBSERVATION`.
3. **Deterministic Deduplication**:
   - If identical observations (`trade_date`, `unit_price`) appear multiple times, they are deduplicated deterministically.
   - If conflicting prices appear on the same `trade_date`, a `DUPLICATE_CONFLICT` diagnostic is recorded and the snapshot degrades to `DataStatus.PARTIAL`.

---

## 4. Symbology & Identity Preflight Gate

1. **Allowed Instrument Types**:
   - `InstrumentType.TEFAS_FUND` (generic fund fallback)
   - `InstrumentType.TEFAS_MONEY_MARKET`
   - `InstrumentType.TEFAS_EQUITY`
   - `InstrumentType.TEFAS_VARIABLE`
   - `InstrumentType.TEFAS_BALANCED`
   - Non-TEFAS instruments (`UCITS_FUND`, `BIST_STOCK`, `US_STOCK`, `GOLD`, `CRYPTO`) are rejected before HTTP dispatch with `UNSUPPORTED_INSTRUMENT_TYPE`.
2. **Dual Identity Preflight**:
   - If both `canonical_instrument_id` and `provider_symbol` are provided, they must resolve to each other under `InstrumentResolverService`. Mismatches fail before HTTP with `IDENTITY_MISMATCH`.
3. **Currency Authority & TRY Capability Boundary**:
   - Currency is sourced strictly from `InstrumentRecord.currency` in the Instrument Master.
   - **MVP Policy (`PUBLIC_TEFAS_PRICE_SUPPORTED_CURRENCY = Currency.TRY ONLY`):** Because the public TEFAS JSON API exposes only undiscriminated reference prices without pay-group identifiers, Sentinax supports public TEFAS price authority **only for canonical TRY instruments**.
   - Any non-TRY canonical instrument (e.g. `USD`, `EUR`, `GBP`) fails closed before HTTP dispatch with `DataStatus.UNAVAILABLE` and warning `AMBIGUOUS_PAY_GROUP_CURRENCY`.
   - **No Synthetic FX Pricing:** Sentinax strictly avoids synthesizing foreign share-class prices via exchange rate conversion (`TRY price / FX rate`) in the absence of explicit dated share-class authority.
4. **Metadata Immutability**:
   - TEFAS `fonUnvan` title is captured as snapshot metadata only and **never mutates or controls** canonical master instrument identity.
5. **Multi-Pay-Group / Share-Class Fail-Closed Gate**:
   - Multi-pay-group funds (e.g. `TPJ` with A Group TRY and B Group USD) return only a single reference price per date on public TEFAS endpoints.
   - Canonical TRY instruments representing the primary TRY reference class remain supported.
   - Canonical non-TRY share-class instruments fail closed until a dedicated share-class identity framework is implemented.

---

## 5. Error & Outage Resiliency

- **Error Envelope Handling**: Responses containing `errorCode` or `errorMessage` are mapped to `DataStatus.UNAVAILABLE` with `ERROR_ENVELOPE`.
- **Empty Responses**: `resultList: []` is mapped to `DataStatus.UNAVAILABLE` with `EMPTY_RESPONSE`.
- **Rate Limiting**: HTTP 429 returns `DataStatus.UNAVAILABLE` with `RATE_LIMITED` and `is_rate_limited=True`.
- **Access Blocks**: HTTP 403 returns `DataStatus.UNAVAILABLE` with `ACCESS_BLOCKED`.
- **Server Errors / Timeouts**: HTTP 500 / timeouts return `DataStatus.UNAVAILABLE` without unhandled exceptions.
