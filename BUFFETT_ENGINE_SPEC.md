# 🧠 Buffett Stock Selection Engine (BIST) - System Specification & Implementation Plan

## 1. 🎯 Executive Summary & Purpose
Bu belge, Sentinax platformuna eklenecek olan "Buffett Stock Selection Engine" (Buffett Hisse Seçim Motoru) için mimari tasarım, kurallar bütünü ve "source of truth" referans belgesidir. 
Amacı: Borsa İstanbul (BIST 100) şirketlerini Warren Buffett ve Berkshire Hathaway yatırım prensiplerine (değer yatırımı, ekonomik hendek, yüksek kârlılık, güvenli bilanço ve güvenlik marjı) göre analiz edip, matematiksel ve deterministik bir skorlama (0-100) ile maksimum 10 hisselik bir portföy oluşturmaktır.

## 2. 🏛️ Mimari Tasarım & Veri Kaynağı (Data Source)

### 2.1. Veri Kaynağı Kararı: Fintables API / İş Yatırım API (Hibrit Yaklaşım)
**Karar:** KAP (Kamuyu Aydınlatma Platformu) verileri "Source of Truth" olmakla birlikte, KAP'tan doğrudan XBRL/PDF kazımak (scraping) çok kırılgan, maliyetli ve bakım yükü (maintenance overhead) yüksek bir işlemdir. Bu nedenle yapılandırılmış finansal veriler için **İş Yatırım API veya Fintables API** gibi güvenilir 3. parti veri sağlayıcıları kullanılacaktır.
**Event-Driven (Tetikleyici) Yapı:** Sistemin kör bir takvimle çalışmaması için, KAP'ın RSS veya Twitter/Telegram botları üzerinden "Yeni Bilanço Açıklandı" bildirimleri dinlenecek (Webhook/Event Listener). İlgili şirketin bilançosu düştüğü anda, sistem API üzerinden taze veriyi çekip yeniden değerleme (re-valuation) sürecini tetikleyecektir.

### 2.2. Enflasyon Muhasebesi (TMS 29) & Reel Hesaplama
Türkiye'deki yüksek enflasyon, nominal büyümeyi illüzyona çevirir. Tüm finansal rasyolar TÜFE (CPI) ile enflasyondan arındırılacaktır (Real Growth).
- **Revenue, EPS, Net Income, FCF:** YoY büyüme hesaplanırken, baz yılın değerleri açıklanan enflasyon oranı kadar yukarı revize edilecek (veya cari değerler deflatör ile indirgenecektir).
- **Örnek:** Nominal Net Kâr büyümesi %50, Enflasyon %65 ise Reel Büyüme negatiftir. Bu şirket büyüme kriterinden kalacaktır.

## 3. ⚙️ Buffett Motoru Kuralları & Kriterleri

Motor tamamen Python fonksiyonları (deterministik) ile çalışacak, LLM yalnızca kalitatif "Marka Gücü/Moat" yorumlamasında kullanılacaktır. Tüm finansal hesaplamalar LLM'den izole bir şekilde `backend/engine/buffett/` altında kodlanacaktır.

### 3.1. KRİTER 1: İş Modeli & Ekonomik Hendek (Moat)
- **Deterministik Proxy'ler:** 
  - Uzun Dönem Brüt Kâr Marjı (Gross Margin) > Sektör Ortalaması.
  - Sürdürülebilir ROIC (Return on Invested Capital) > WACC.
  - 10 yıllık dönemde istikrarlı EPS büyümesi.
- **LLM Katmanı:** Şirketin pricing power (fiyat belirleme gücü) ve marka değeri, haberler ve faaliyet raporları özetlenerek LLM tarafından 1-10 arası kalitatif olarak skorlanır.

### 3.2. KRİTER 2: Kârlılık (Profitability)
- **Ana Eşik:** Son 5-10 yıllık ortalama **ROE (Özkaynak Kârlılığı) > %15** olmalıdır.
- **Kaldıraç Kontrolü:** Yüksek ROE'nin aşırı borçtan (DuPont analizi) kaynaklanmadığı teyit edilir.
- **FCF Conversion:** Net Kârın, Serbest Nakit Akışına (FCF) dönüşüm oranı pozitif ve yüksek olmalıdır (Kâğıt üstünde değil, kasada nakit yaratan şirketler).

### 3.3. KRİTER 3: Bilanço Sağlamlığı
- **Ana Eşik:** **Debt / Equity (Borç / Özkaynak) < 0.50**.
- **İstisna (MVP Bypass):** Bankalar, Sigorta Şirketleri, Aracı Kurumlar ve Holdingler (Finansal Sektör) yüksek kaldıraçla çalıştıkları için klasik D/E formülüne uymazlar. MVP (Minimum Viable Product) aşamasında bu sektörler analiz dışı bırakılacaktır (Hard Bypass).

### 3.4. KRİTER 4: Güvenlik Marjı (Margin of Safety) & Değerleme
- **Kural:** `Market Price <= Intrinsic Value * 0.75` (%25 İskonto).
- **Metodoloji (DCF):** Enflasyonist ortama uygun, Türkiye risk primi (ERP) ve risksiz getiri oranı (10 yıllık tahvil) kullanılarak WACC hesaplanır.
- **Senaryolar:** Base (Beklenen), Bear (Kötü), Bull (İyi) senaryolarıyla İndirgenmiş Nakit Akışı (DCF) hesaplanır. Muhafazakar (Base-Bear arası) bir Intrinsic Value baz alınır.

## 4. 🧮 Skorlama, Seçim & Portföy Yönetimi

### 4.1. Scoring Model (0-100)
Şirketler 4 ana kategoride puanlanır (Çifte sayım yapılmaz):
1. **Moat / Kalite:** %20 ağırlık
2. **Kârlılık:** %30 ağırlık
3. **Bilanço:** %20 ağırlık
4. **Değerleme (Margin of Safety):** %30 ağırlık

**Veto (Gate) Mekanizması (Hard Fail):**
- Negatif özkaynak.
- Son 3 yılın 2'sinde net zarar.
- D/E > 1.5 (Finans dışı sektörler için).
Bu kriterlere takılan şirketlerin skoru direkt `0` olur ve anında elenir.

### 4.2. Selection Layer & Max 10 Kuralı
- **Ayrım:** Skorlama katmanı her şirkete bir puan verir, "Selection Layer" ise portföyü oluşturur.
- **Kural:** Portföy **maksimum 10 hisse** ile sınırlıdır.
- Eğer sadece 4 hisse tüm Buffett kriterlerini ve Güvenlik Marjını sağlıyorsa, portföy 4 hissede kalır. Listeyi 10'a tamamlamak için kalite standartları KESİNLİKLE gevşetilmez.

### 4.3. Snapshot, Loglama & Portföy Değişimi
- **Snapshot:** Her bilanço dönemindeki (veya tetiklenen) değerlendirme, Supabase üzerinde bir snapshot olarak (örneğin `buffett_snapshots` tablosu) saklanır.
- **Audit Trail:** Bir hisse portföye girdiğinde (ADDED) veya çıktığında (REMOVED), bunun nedeni matematiksel olarak veri tabanına kaydedilir (Örn: "Margin of safety %25'in altına düştüğü için satıldı", "ROE %15'in altına gerilediği için çıkarıldı").

### 4.4. Backtest & Data Confidence
- **Data Confidence Score (0-100):** Şirketin tarihsel verilerinde eksiklik varsa (örn. sadece 3 yıllık geçmişi olan yeni halka arz), Data Confidence Score düşürülür. Eksik veri `0` olarak kabul edilmez, analiz güvenilirliği düşürülür (Confidence < %70 ise portföye alınmaz).
- **Look-ahead Bias Yok:** Backtest süreçlerinde, o tarihte KAP'ta açıklanmamış hiçbir veri modellemeye dahil edilemez. Bilanço açıklanma tarihleri referans alınacaktır.

## 5. 🛠️ Yürütme ve Geliştirme Fazları (Implementation Plan)

- **Faz 1 (Data & Infrastructure):** İş Yatırım/Fintables API entegrasyonu, Event-Listener (Yeni bilanço takibi) mekanizmasının kurulması.
- **Faz 2 (Deterministik Motor):** Python sınıflarının (Moat, Profitability, DCF, Balance Sheet) yazılması ve TMS 29 enflasyon düzeltme logiğinin inşası. Modüller `backend/engine/buffett/` dizininde oluşturulacaktır.
- **Faz 3 (Scoring & Selection):** 0-100 Skor modelinin birleştirilmesi, Max 10 kuralını işleten Selection Manager modülünün tasarlanması.
- **Faz 4 (LLM & Database Integration):** Groq/Gemini ile nitel Moat analizi entegrasyonu (Zero-Trust kurallarına uygun olarak PII/maskeleme ile), Supabase tablolarının (`buffett_snapshots`) oluşturulması ve geçmişin loglanması.
- **Faz 5 (Testing):** `pytest-socket` izolasyonu ile look-ahead bias olmadan backtest ve birim (unit) testlerinin yazılması. Gerçek ağ çağrılarının mocklanması.
