# 🛑 Stooq Global Historical EOD — Production Feasibility Gate Review

## 1. Executive Summary & Verdict

| Assessment Field | Result |
| :--- | :--- |
| **Provider Candidate** | **Stooq** (`stooq.com`) |
| **Audit Date** | August 27, 2026 |
| **Proposed Role** | Global Historical EOD (US/EU Stocks & ETFs long-horizon history >= 5Y) |
| **Access Classification** | **`RED`** (Unsuitable for automated production backend) |
| **Final Feasibility Verdict** | **`FAIL` (NO-GO)** |

---

## 2. Technical Findings & Live Access Inspection

### 2.1 Current No-Key & Anti-Bot Behavior
- Direct HTTP requests to the commonly referenced historical CSV endpoint (`https://stooq.com/q/d/l/?s=aapl.us&i=d`) return `HTTP/1.1 200 OK` containing a client-side **JavaScript Proof-of-Work (PoW) Anti-Bot Challenge** rather than CSV data.
- The challenge executes a client-side SHA-256 hash puzzle (`crypto.subtle.digest`) before posting to `/__verify` to obtain a session cookie. Standard HTTP clients (e.g., `httpx`, `curl`, `aiohttp`) receive the HTML challenge payload and 0 observations.
- `robots.txt` explicitly disallows all automated crawlers:
  ```text
  User-agent: Bingbot
  Allow: /

  User-agent: Googlebot
  Allow: /

  User-agent: *
  Disallow: /
  ```

### 2.2 API Key & Developer Portal
- Stooq does **not** provide an official public API, developer portal, or self-service API key provisioning system.
- Community third-party scrapers rely on browser emulation, session sniffing, or manual bulk zip file downloads.

### 2.3 Quota & Error Handling
- Quotas are **undisclosed** (`UNKNOWN`).
- Scraping triggers undisclosed IP rate limits ("Exceeded the daily hits limit") followed by immediate anti-bot challenges and IP blocks.

### 2.4 Price Adjustment & Corporate Action Semantics
- **Adjustment Default**: Web downloads on Stooq default to **split-adjusted** and **dividend-adjusted** prices.
- **Raw Price Isolation**: The endpoint does not cleanly separate unadjusted raw daily prices without manual browser UI interactions.
- **Volume Semantics**: `UNKNOWN` (unspecified whether volume is adjusted alongside splits).
- **Semantics Classification**: `PRICE_ADJUSTMENT_SEMANTICS_UNVERIFIED`.

### 2.5 Point-in-Time (PIT) Semantics
- Stooq provides ex-post adjusted retrospective snapshots.
- Does not expose `published_at` or point-in-time publication timestamps.

---

## 3. Compliance & Reliability Conclusion

1. **Anti-Bot Policy Violation**: Sentinax architecture forbids bypassing anti-bot challenges (CAPTCHA, JavaScript PoW miners, or Cloudflare/OpenResty gates).
2. **Robots.txt Conflict**: Programmatic scraping directly violates Stooq's `User-agent: * Disallow: /` directive.
3. **No Production SLA**: Lack of a formal API and commercial SLA creates severe reliability risks for institutional-grade portfolio risk engines.

**Recommendation**: Do not implement a Stooq adapter (`stooq.py`). Retain Alpha Vantage for validated low-volume EOD ingestion, and evaluate official commercial API providers (e.g. EODHD, Tiingo, Polygon) for long-horizon multi-year risk history in subsequent phases.
