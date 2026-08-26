# backend/deprecated — Deprecated Module Archive

Bu klasör, Sentinax Private Engine kapsamı dışında kalan ve
Red Team / mimari review sonucunda üretim yolundan çıkarılan modülleri içerir.

## İçindekiler

| Dosya | Neden Deprecated |
|-------|-----------------|
| `optimization_engine.py` | Monte-Carlo max_sharpe/min_vol/max_return üretiyor. Red Team sonucunda kabul edilmemiş metodoloji. Gelecekte HRP/HERC/CVaR ile sıfırdan yazılacak. |
| `ml_predictor.py` | Gerçekten ML değil — EMA/momentum + yfinance. Crypto-specific kod içeriyor. Production API path'inden kaldırıldı. İleride experimental/shadow olarak sıfırdan ele alınacak. |
| `shadow_pnl_tracker.py` | Paper-trade semantics (BUY/SELL order simulation). Decision-support scope'una aykırı. PnL hesaplama mantığı ileride Private Engine için yeniden tasarlanacak. |

## Kurallar

- Bu klasörden **aktif kodda import YOK**.
- Yeni geliştirmelerde bu dosyalara referans verme.
- Git geçmişi tam bağlamı barındırıyor.
