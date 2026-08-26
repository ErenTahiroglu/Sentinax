"""
🤖 ML Fiyat Tahmin Modülü — P5 Öncelik Matrisi (Hafif Projeksiyon Sürümü)
========================================================================
Pandas ve Numpy tabanlı hareketli ortalama (EMA) ve momentum projeksiyonu.
Prophet kütüphanesinin ağırlığını (RAM/Depolama) sistemden kaldırmak için tasarlanmıştır.

• Varsayılan KAPALI — ML_PREDICTION_ENABLED=true env var ile açılır.
• Sanayi Standartı Projeksiyon — Son 90 günlük Momentum + Volatilite.
"""
import logging
import os
from typing import cast, Any
import numpy as np

logger = logging.getLogger(__name__)

ML_ENABLED: bool = os.getenv("ML_PREDICTION_ENABLED", "false").lower() == "true"


def predict_price(ticker: str) -> dict:
    """
    Ticker için 7 günlük matematiksel fiyat projeksiyonu üretir.

    Returns:
        {
            "ticker": "AAPL",
            "direction": "UP" | "DOWN" | "SIDEWAYS",
            "confidence": 75.0,
            "current_price": 182.5,
            "target_7d": 191.2,
            "change_pct": 4.77,
            "plot_data": [{"ds": "2026-03-25", "yhat": 191.2, "yhat_lower": 185.0, "yhat_upper": 197.4}],
            "model": "EMA-Momentum",
            "error": null
        }
    """
    if not ML_ENABLED:
        return {
            "ticker": ticker,
            "error": "ML tahmini devre dışı. ML_PREDICTION_ENABLED=true ile aktifleştirin.",
            "enabled": False
        }

    try:
        import pandas as pd
        import yfinance as yf # type: ignore[import]

        logger.info(f"📊 Fiyat Projeksiyonu başlatılıyor: {ticker}")

        # ── 1. Tarihsel Veri ──────────────────────────────────────────────
        stock = yf.Ticker(ticker)
        hist = stock.history(period="90d", interval="1d")

        if hist.empty or len(hist) < 20:
            return {"ticker": ticker, "error": "Yetersiz tarihsel veri (min 20 gün).", "enabled": True}

        # Tarih ve Kapanış Verilerini Al
        df = pd.DataFrame({
            "ds": cast(pd.DatetimeIndex, hist.index).tz_localize(None),
            "y": hist["Close"].values,
        })
        
        if df.empty or "y" not in df.columns:
            return {"ticker": ticker, "error": "Boş DataFrame döndü.", "enabled": True}
        
        # Tamamlanmamış Mum Koruması (Incomplete Candle)
        today = pd.Timestamp.now().normalize()
        if df["ds"].iloc[-1].normalize() == today:
            df = df.iloc[:-1]

        if len(df) < 20:
            return {"ticker": ticker, "error": "Temizlik sonrası yetersiz veri.", "enabled": True}

        current_price = float(df["y"].iloc[-1])

        # ── 2. Veri Zehirlenmesi / Anomali Filtresi (Outlier Detection) ──
        returns = df["y"].pct_change().dropna()
        # %30'dan fazla günlük değişimler (kriptolar hariç) şüpheli veri pikidir (split/hata)
        is_crypto = ticker.endswith("-USD")
        anomaly_threshold = 0.50 if is_crypto else 0.25 
        
        anomalies = returns[returns.abs() > anomaly_threshold]
        if not cast(pd.Series, anomalies).empty:
             logger.warning(f"⚠️ [{ticker}] Veri Zehirlenmesi/Anomali Tespit Edildi! (%{anomaly_threshold*100}+ devingenlik). Tahmin durduruluyor.")
             return {
                 "ticker": ticker, 
                 "error": f"Veri setinde aşırı anomali tespit edildi (%{round(float(cast(pd.Series, anomalies).iloc[-1])*100,2)}). Tahmin askıya alındı.",
                 "enabled": True
             }

        series = df["y"]
        
        # EMA (Üstel Hareketli Ortalama) - Son trendi yakalamak için
        ema_12 = series.ewm(span=12, adjust=False).mean()
        curr_ema = float(ema_12.iloc[-1])
        
        # Momentum: Son 10 günlük eğilim (Doğrusal Regresyon Eğimi benzeri basit trend)
        if len(series) >= 10:
            momentum = float(cast(pd.Series, series).iloc[-1] - cast(pd.Series, series).iloc[-10]) / 10.0
        else:
            momentum = float(cast(pd.Series, series.diff()).mean())
            
        if pd.isna(momentum):
            momentum = 0.0

        # Volatilite (Günlük getiri standart sapması)
        daily_vol_val = returns.std() if not returns.empty else 0.0
        daily_vol = float(cast(Any, daily_vol_val)) # Günlük oynaklık oranı (Örn: 0.02)
        if pd.isna(daily_vol) or daily_vol < 0:
            daily_vol = 0.02 # Safe baseline volatility
        
        # ── 3. 7 Günlük Projeksiyon ───────────────────────────────────────
        proj_days = 7
        
        # Gelecekteki Tarihleri Üret (Kripto için Gün, Hisse için İş günü)
        freq = "D" if is_crypto else "B"
        future_dates = pd.date_range(start=df["ds"].max() + pd.Timedelta(days=1), periods=proj_days, freq=freq)
        
        plot_data = []
        target_price = current_price
        
        # Güven aralığı genişlemesi için katsayı (Hata payı)
        # Volatilitenin gün sayısı kareköküyle büyümesi kuralı
        for i in range(1, proj_days + 1):
            # Ağırlıklı kombinasyon: EMA'ya doğru çekilme + Mevcut Momentum
            step_target = target_price + momentum + (curr_ema - target_price) * 0.1
            
            # Volatilite Bantları (Standart Hata)
            std_error = current_price * daily_vol * np.sqrt(i)
            
            plot_data.append({
                "ds": str(cast(Any, future_dates[i-1]).date()),
                "yhat": round(float(step_target), 2),
                "yhat_lower": round(float(step_target - std_error), 2),
                "yhat_upper": round(float(step_target + std_error), 2),
            })
            target_price = step_target # Zincirleme

        target_price = plot_data[-1]["yhat"]
        lower_bound = plot_data[-1]["yhat_lower"]
        upper_bound = plot_data[-1]["yhat_upper"]
        change_pct = ((target_price - current_price) / current_price) * 100

        # ── 4. Güven Skoru (Confidence) & Drift Fallback ───────────────────
        vol_factor = daily_vol * 100
        
        if vol_factor <= 0:
            confidence = 95.0
        else:
            confidence = max(10, min(95, 100 - (vol_factor * 15)))

        # ⚡ DRIFT GUARD: Güven skoru fırlarsa/çürürse klasik analizlere dön (Fallback)
        if confidence < 40.0:
            logger.warning(f"📉 [{ticker}] Model Drift / Denge Bozulması! Güven Skoru: %{confidence}. Tahmin reddediliyor.")
            return {
                "ticker": ticker,
                "error": f"Yüksek oynaklık nedeniyle tahmin güvenilir değil (%{round(confidence,1)}). Klasik analize güveniniz.",
                "enabled": True
            }

        # ── 5. Yön Belirleme ─────────────────────────────────────────────
        if change_pct > 1.0:
            direction = "UP"
        elif change_pct < -1.0:
            direction = "DOWN"
        else:
            direction = "SIDEWAYS"

        return {
            "ticker": ticker,
            "direction": direction,
            "confidence": round(confidence, 1), # 0-100 ölçeği
            "current_price": round(current_price, 2),
            "target_7d": round(target_price, 2),
            "lower_7d": round(lower_bound, 2),
            "upper_7d": round(upper_bound, 2),
            "change_pct": round(change_pct, 2),
            "plot_data": plot_data,
            "model": "EMA-Momentum",
            "error": None,
            "enabled": True,
        }

    except Exception as e:
        logger.error(f"Projeksiyon hatası ({ticker}): {e}")
        return {"ticker": ticker, "error": str(e), "enabled": True}
