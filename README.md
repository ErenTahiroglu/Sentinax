# Sentinax — Investment Decision-Support Platform

Sentinax, bireysel yatırımcılar için tasarlanmış bir **yatırım karar destek platformudur**.
Otonom portföy yöneticisi değildir, emir göndermez, pozisyon açmaz.
Yatırımcının kendi kararlarını daha iyi verebilmesi için analiz, veri ve bağlam sunar.

![License](https://img.shields.io/badge/License-MIT-purple.svg)
![Architecture](https://img.shields.io/badge/Architecture-FastAPI_%2B_LangGraph-blue.svg)
![Tests](https://img.shields.io/badge/Tests-Pytest-brightgreen.svg)

---

## Kapsam

### ✅ Desteklenen Varlık Evreni

- **BIST Hisseleri** (Türk borsası)
- **TEFAS Fonları** (Para piyasası, hisse, değişken)
- **ABD Hisseleri ve ETF'leri** (NYSE, NASDAQ)
- **Avrupa Hisseleri ve ETF'leri**
- **Altın ve gümüş** (ALTIN.S1 ve benzeri)
- **Döviz çiftleri** (USD/TRY, EUR/TRY)
- **TL tahvil/bono ve Türkiye Eurobondları**

### ❌ Kapsam Dışı

- **Kripto para (YOK)** — Bitcoin, Ethereum ve tüm kripto varlıklar kapsam dışıdır
- **Emir iletimi** — Sistem hiçbir zaman alım-satım emri göndermez
- **Paper trading** — Sanal emir simülasyonu yoktur
- **ML tahmin motoru** — İleride experimental/shadow olarak değerlendirilecek

---

## İki Bounded Context

### 1. Public Buffett Engine

Buffett metodolojisine (Graham çizgisi) dayalı değer yatırımı tarayıcısı.
BIST hisselerini kalite, moat gücü, bilanço ve değerleme metriklerine göre puanlar.

**Endpoint:** `/buffett/portfolio`, `/buffett/history`

### 2. Private Personal Investment Decision Engine *(Foundation)*

Kullanıcının kendi portföyü üzerinde kişiselleştirilmiş yatırım karar analizi.

**Temel prensipler:**
- Eksik veri ≠ sıfır — `DataStatus.UNAVAILABLE` değeri `None`'dır, asla 0 değil
- `PARTIAL` analiz geçerli ve yayınlanabilir bir sonuçtur
- Veri uydurma yasaktır
- Her veri noktası için kaynak + tarih izlenebilirliği (`ProviderProvenance`)

---

## Teknoloji Yığını

| Katman | Teknoloji |
|--------|----------|
| Backend | Python 3.x, FastAPI |
| AI Orkestrasyon | LangGraph, LangChain |
| Veritabanı | Supabase (PostgreSQL) |
| Cache / Rate-limit | Redis (Upstash) |
| Frontend | Vanilla JS, HTML5, CSS3 |
| Deployment | Vercel (Frontend), Render (Backend) |
| Testler | Pytest, Puppeteer |

---

## Mimari Özeti

```
sentinax/
├── backend/
│   ├── api/           FastAPI app, tek aktif router: /buffett
│   ├── analyzers/     BIST, US, teknik analizörler
│   ├── data/          Veri kaynakları, market detector
│   ├── deprecated/    Devre dışı bırakılan modüller (üretim pathinde değil)
│   ├── engine/
│   │   ├── buffett/   Public Buffett Engine (tamamlandı)
│   │   └── private/   Private Engine bounded context (foundation)
│   ├── infrastructure/ Redis, Supabase, LLM factory, auth
│   ├── nodes/         LangGraph node'ları
│   └── tests/         Pytest test suite (Zero-Trust)
└── frontend/
    └── buffett/       Public Buffett UI
```

---

## Hızlı Başlangıç

```bash
# Backend
uvicorn backend.main:app --reload

# Frontend (static serve)
python -m http.server 3000 --directory frontend/

# Docker Compose
docker-compose up --build

# Testler
pytest backend/tests/ -v
```

---

## Güvenlik

- Tüm API uç noktaları JWT koruması altındadır
- Rate limiting ve Idempotency Middleware aktif
- Zero-Trust test izolasyonu: `pytest-socket` ile ağ erişimi engellenir
- PII sanitizasyonu LLM prompt'larına veri girmeden önce uygulanır

---

**Lisans:** MIT  
* **Sponsorluk:** Katkıda bulunmak için [GitHub Sponsor](https://github.com/sponsors/ErenTahiroglu) sayfasını ziyaret edebilirsiniz.
