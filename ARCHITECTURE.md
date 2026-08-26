# Sentinax — Architecture v5.0

**Revizyon:** 2026-08-26  
**Önceki sürüm:** v4.0 (otonom portföy yöneticisi — kaldırıldı)

---

## Temel İlkeler

1. **Karar destek, emir değil.** Sistem asla BUY/SELL emri göndermez.
2. **Eksik veri ≠ sıfır.** `DataStatus.UNAVAILABLE` → `None`. Asla uydurma.
3. **Kripto kapsam dışı.** `detect_market()` kripto için `UNKNOWN` döner.
4. **PARTIAL geçerli sonuçtur.** Sistem eksik inputla çökmez.
5. **Point-in-time bütünlüğü.** `effective_date` ≠ `retrieved_at`.
6. **Zero-Trust test izolasyonu.** `pytest-socket` ağ erişimini engeller.

---

## İki Bounded Context

### Context A — Public Buffett Engine

```
frontend/buffett/          → Static UI
backend/api/routers/buffett.py  → FastAPI router (/buffett/*)
backend/engine/buffett/    → Orchestrator, data_fetcher, valuation, scoring
```

**Bağımsız modül.** Diğer engine'lerden izole.  
Giriş: ticker listesi + CPI verisi  
Çıkış: puanlanmış portföy (moat, profitability, balance_sheet, valuation, DCF)

### Context B — Private Personal Investment Decision Engine *(Foundation)*

```
backend/engine/private/
├── __init__.py            → Scope dokümantasyonu
├── domain.py              → Core domain enums (AssetClass, DataStatus, vb.)
├── result.py              → DataResult, AnalysisResult contracts
└── provider_contract.py   → DataProviderContract Protocol, ProviderResponse
```

**Mevcut durum:** Domain contract katmanı tamamlandı.  
**Sonraki faz:** Data provider implementasyonları (BIST, TEFAS, SEC, EVDS).

---

## Üç Katmanlı Mimari

```
┌─────────────────────────────────────────────────────────┐
│  PRESENTATION                                           │
│  frontend/buffett/  (Vanilla JS, HTML5, CSS3)           │
└─────────────────────────────────────────────────────────┘
                        │ HTTP / WebSocket
┌─────────────────────────────────────────────────────────┐
│  APPLICATION (FastAPI)                                  │
│                                                         │
│  /buffett/*   ← BuffettEngine orchestrator              │
│  /api/health  ← Liveness probe                          │
│  /ws          ← WebSocket                               │
│                                                         │
│  Middleware: CORS · GZip · Idempotency · Correlation-ID │
│  Auth: JWT (Supabase)  Rate-limit: Redis (Upstash)      │
└─────────────────────────────────────────────────────────┘
                        │
┌─────────────────────────────────────────────────────────┐
│  DATA                                                   │
│  Supabase (PostgreSQL) · Redis (Upstash)                │
│  yfinance / yahooquery (market data)                    │
│  TEFAS scraper (fund data)                              │
└─────────────────────────────────────────────────────────┘
```

---

## LangGraph Agent Pipeline (Old Public Engine)

Eski CIO orchestrator (multi-agent debate) şu anda `graph.py` içinde
hâlâ tanımlı fakat aktif router tarafından kullanılmıyor.
`chat_orchestrator.py` bu graph'ı import ediyor.

```
START → MarketDataNode → NewsNode → IslamicNode(no-op)
     → InvestmentDebate(Bull/Bear/Neutral/PM)
     → RiskDebate (conditional)
     → OutputMapper → END
```

**Not:** IslamicNode graph'ta no-op stub olarak bırakıldı.
Gelecekte bu agent pipeline Private Engine için yeniden tasarlanacak.

---

## Data Flow — Private Engine (Foundation)

```
Instrument ID
    │
    ▼
DataProviderContract.fetch()
    │
    ▼
ProviderResponse
    │── provider_name
    │── source_quality (SourceTier)
    │── retrieved_at  (wall-clock UTC)
    │── effective_date (economic date ≠ retrieved_at)
    │── status (DataStatus)
    │── raw (unmodified payload)
    │
    ▼
DataProviderContract.normalize()
    │
    ▼
DataProviderContract.validate()  → warnings: list[str]
    │
    ▼
DataResult
    │── value (None if UNAVAILABLE — never 0)
    │── status (COMPLETE | PARTIAL | DEGRADED | STALE | UNAVAILABLE)
    │── confidence (HIGH | MEDIUM | LOW | NONE)
    │── as_of (date)
    │── source_refs
    │── warnings
    │── missing_inputs
    │
    ▼
AnalysisResult
    │── components: dict[str, DataResult]
    │── status (derived from components)
    │── computed_at
    └── global_warnings
```

---

## Deprecated Modules

Üretim yolundan çıkarılmış modüller `backend/deprecated/` altında arşivlenmiştir.

| Modül | Sebep |
|-------|-------|
| `optimization_engine.py` | Monte-Carlo MVO — yanlış metodoloji. HRP/CVaR ile yeniden yazılacak. |
| `ml_predictor.py` | Gerçek ML değil. Crypto bağımlılıkları. |
| `shadow_pnl_tracker.py` | Paper-trade semantics. Kapsam dışı. |

**Kural:** `from backend.deprecated.*` aktif kodda yasaktır.  
`test_no_crypto_path.py` bunu her CI çalışmasında doğrular.

---

## Güvenlik Mimarisi

```
API Request
    │
    ├─ CorrelationIdMiddleware  (tracing)
    ├─ IdempotencyMiddleware    (POST/PUT/PATCH dedup)
    ├─ NoCacheMiddleware        (UI routes)
    ├─ GZipMiddleware           (>1KB)
    ├─ CORSMiddleware           (allowlist)
    │
    ├─ JWT Auth (Supabase)      (per-endpoint dependency)
    ├─ Rate Limiter (Redis)     (per-endpoint dependency)
    │
    └─ Handler
```

### LLM Güvenliği

- **PII Sanitizasyonu:** Kullanıcı verileri LLM prompt'larına girmeden önce temizlenir
- **XML Tag İzolasyonu:** Harici veriler `<news_item>`, `<context>` tag'leri ile izole edilir
- **Mocked LLM (Test):** `conftest.py` tüm LLM çağrılarını mock'lar

---

## Test Stratejisi

```bash
# Tüm testler
pytest backend/tests/ -v --disable-socket

# Private engine domain
pytest backend/tests/test_private_domain.py -v

# Structural regression guard (no crypto, no deprecated imports)
pytest backend/tests/test_no_crypto_path.py -v

# Buffett engine
pytest tests/test_buffett_engine.py -v
```

**Zero-Trust kuralı:**  
`pytest-socket` tüm network erişimini engeller.  
Harici API çağrıları mock edilmeli; aksi hâlde test isolation hatası alınır.

---

## Klasör Yapısı (Tam)

```
sentinax/
├── backend/
│   ├── analyzers/
│   │   ├── base_analyzer.py
│   │   ├── bist_analyzer.py       ← BIST + TEFAS
│   │   ├── technical_analyzer.py
│   │   └── us_analyzer.py
│   ├── api/
│   │   ├── main.py               ← FastAPI app
│   │   ├── models.py
│   │   ├── config.py
│   │   ├── dependencies.py
│   │   ├── websocket.py
│   │   └── routers/
│   │       └── buffett.py        ← Single active router
│   ├── data/
│   │   ├── constants.py
│   │   ├── data_sources.py
│   │   ├── market_detector.py    ← BIST/TEFAS/US/UNKNOWN (no CRYPTO)
│   │   ├── news_fetcher.py
│   │   └── tefas_scraper.py
│   ├── deprecated/               ← Archive (not in production path)
│   │   ├── optimization_engine.py
│   │   ├── ml_predictor.py
│   │   └── shadow_pnl_tracker.py
│   ├── engine/
│   │   ├── agent_states.py
│   │   ├── circuit_breaker.py
│   │   ├── graph.py              ← LangGraph pipeline (old engine)
│   │   ├── buffett/              ← Public Buffett Engine
│   │   └── private/              ← Private Engine (foundation)
│   │       ├── domain.py
│   │       ├── result.py
│   │       └── provider_contract.py
│   ├── infrastructure/           ← Redis, Supabase, LLM, auth, metrics
│   ├── nodes/
│   │   ├── adversarial_agents.py
│   │   ├── ai_agent.py
│   │   └── data_nodes.py         ← Market + News nodes (no crypto path)
│   ├── services/
│   │   ├── analysis_service.py   ← Placeholder (optimization removed)
│   │   └── chat_orchestrator.py
│   ├── tests/
│   │   ├── conftest.py           ← Zero-Trust mocking
│   │   ├── test_private_domain.py
│   │   ├── test_no_crypto_path.py
│   │   ├── test_market_detector.py
│   │   └── ... (other tests)
│   └── utils/
├── docs/
│   └── FOUNDATION_AUDIT.md
├── frontend/
│   └── buffett/                  ← Public Buffett UI
└── infrastructure/               ← Docker, CI/CD
```
