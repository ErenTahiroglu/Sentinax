import asyncio
import os
import httpx
import logging
from datetime import datetime, timezone
from functools import wraps

from backend.services.chat_orchestrator import orchestrator


logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

def dlq_guard(func):
    """
    Hatalı arka plan görevlerini yakalar ve Redis DLQ bültenine fırlatır.
    """
    @wraps(func)
    async def wrapper(*args, **kwargs):
        ticker = kwargs.get("ticker") or (args[1] if len(args) > 1 else "Unknown")
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            logger.error(f"🚨 [DLQ Trigger] Görev Hatası ({ticker}): {e}")
            try:
                from backend.infrastructure.redis_cache import cache_get, cache_set
                dlq_key = "dlq:failed_jobs"
                failed_jobs = cache_get(dlq_key) or {}
                
                failed_jobs[ticker] = {
                    "error": str(e),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "retry_suggested": True
                }
                # 24 saatliğine listeyi tut
                cache_set(dlq_key, failed_jobs, ttl=86400)
                logger.info(f"📌 [DLQ] Ticker {ticker} hatası Redis'e kaydedildi.")
            except Exception as redis_err:
                 logger.warning(f"DLQ Redis kayıt hatası: {redis_err}")
                 
            # Döngünün çökmemesi için hatayı yut (veya hata döngüde de yutuluyor)
            return {"error": str(e)}
            
    return wrapper

@dlq_guard
async def _process_single_ticker(ticker, client, headers, user_id, weight, chat_id):
    """Her hisseyi otonom tarayan ana görev mantığı sarmalı."""
    input_state = {
        "ticker": ticker,
        "messages": [f"Otonom tarama: {ticker}"],
        "check_financials": True,
        "check_islamic": True
    }
    result = await orchestrator.ainvoke(input_state)
    res = result.get("final_report", {})

    if not res or ("error" in res and res["error"]):
        return {"error": res.get("error", "Boş yanıt")}
        
    alerts = []
    fin = res.get("financials", {})
    son = fin.get("son_fiyat")
    onceki = fin.get("onceki_kapanis")
    
    portfolio_return_sum = 0.0
    total_weight = 0.0

    if son and onceki and onceki > 0:
        degisim = (son - onceki) / onceki
        portfolio_return_sum += degisim * weight
        total_weight += weight
        if degisim <= -0.05:
            alerts.append(f"{ticker} günlük %{abs(degisim*100):.1f} değer kaybetti!")
            
    # Teknik / Temel Sinyaller
    tech_signals = res.get("technicals", {}).get("signals", [])
    for s in tech_signals:
        if s.get("signal") == "BEARISH" and "MACD" in s.get("reason", ""):
            alerts.append(f"{ticker} teknik analizinde MACD Satım sinyali tespit edildi.")
            
    if res.get("radar_score", {}).get("profitability", 50) < 30:
        alerts.append(f"{ticker} değerlemesinde karlılık skoru alarm seviyesine düştü ({res.get('radar_score', {}).get('profitability')}/100).")
        
    for alert_msg in set(alerts):
        payload = {
            "user_id": user_id, "ticker": ticker, "message": alert_msg,
            "created_at": datetime.now(timezone.utc).isoformat(), "is_read": False
        }
        await client.post(f"{SUPABASE_URL}/rest/v1/alerts", headers=headers, json=payload)
        
        if chat_id and TELEGRAM_BOT_TOKEN:
            tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            try:
                await client.post(tg_url, json={"chat_id": chat_id, "text": f"🔔 Otonom Uyarı ({ticker}):\n{alert_msg}"})
            except Exception as tg_err:
                logger.error(f"Telegram Hatası ({user_id}): {tg_err}")
                
    return {"status": "ok", "return_sum": portfolio_return_sum, "weight": total_weight}

async def start_alert_scheduler():
    """Arka planda otonom çalışan portföy tarayıcısı (Zero Trust - Service Role)."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning("Autonomous Scheduler: Supabase kimlikleri eksik. Uyarı sistemi devre dışı.")
        return

    # 🛡️ Interval Config
    INTERVAL_SECONDS = int(os.getenv("SCHEDULER_INTERVAL_SECONDS", "840")) # Default 14 min
    logger.info(f"Autonomous Scheduler: Otonom tetikleyici aktif. {INTERVAL_SECONDS // 60} Dakikada bir çalışacak.")

    
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json", "Prefer": "return=minimal"
    }

    await asyncio.sleep(60)

    while True:
        try:
            logger.info("Autonomous Scheduler: Piyasayı izlemek üzere taramaya başlanıyor...")
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp_settings = await client.get(f"{SUPABASE_URL}/rest/v1/user_settings", headers=headers)
                user_settings_map = {}
                if resp_settings.status_code == 200:
                    for us in resp_settings.json():
                        user_settings_map[us.get("user_id")] = us

                resp = await client.get(f"{SUPABASE_URL}/rest/v1/legacy_portfolio_watchlists?select=user_id,tickers", headers=headers)
                if resp.status_code != 200:
                    await asyncio.sleep(INTERVAL_SECONDS)
                    continue
                
                portfolios = resp.json()
                for portfolio in portfolios:
                    user_id = portfolio.get("user_id")
                    tickers = portfolio.get("tickers", [])
                    chat_id = user_settings_map.get(user_id, {}).get("telegram_chat_id")
                    
                    if not user_id or not tickers:
                        continue
                    
                    portfolio_return_sum = 0.0
                    total_weight = 0.0
                    
                    for item in tickers:
                        ticker = item.get("ticker", str(item)) if isinstance(item, dict) else item
                        weight = float(item.get("weight", 1.0)) if isinstance(item, dict) else 1.0
                        
                        # 🛡️ DECORATED EXECTION
                        res = await _process_single_ticker(ticker, client, headers, user_id, weight, chat_id)

                        
                        if "status" in res and res["status"] == "ok":
                            portfolio_return_sum += res["return_sum"]
                            total_weight += res["weight"]

                        await asyncio.sleep(4)

                    # --- PORTFOLIO SNAPSHOT (Equity Curve) ---
                    if total_weight > 0:
                        try:
                            avg_weighted_return = portfolio_return_sum / total_weight
                            
                            # Son snapshot'ı çek
                            hist_url = f"{SUPABASE_URL}/rest/v1/portfolio_snapshots?user_id=eq.{user_id}&order=timestamp.desc&limit=1"
                            hist_resp = await client.get(hist_url, headers=headers)
                            
                            prev_total_value = 10000.0  # Varsayılan başlangıç
                            prev_cash = 0.0
                            
                            if hist_resp.status_code == 200:
                                hist_data = hist_resp.json()
                                if hist_data:
                                    prev_total_value = float(hist_data[0].get("total_value", 10000.0))
                                    prev_cash = float(hist_data[0].get("cash_balance", 0.0))
                            
                            # Yeni Toplam Değer Hesabı (Bileşik Getiri)
                            new_total_value = prev_total_value * (1 + avg_weighted_return)
                            
                            # DB'ye yaz
                            snap_payload = {
                                "user_id": user_id,
                                "total_value": round(new_total_value, 2),
                                "cash_balance": round(prev_cash, 2),
                                "timestamp": datetime.now(timezone.utc).isoformat()
                            }
                            await client.post(f"{SUPABASE_URL}/rest/v1/portfolio_snapshots", headers=headers, json=snap_payload)
                            logger.info(f"Snapshot alındı: {user_id} -> {new_total_value:.2f}")
                        except Exception as snap_err:
                            logger.error(f"Snapshot Hatası ({user_id}): {snap_err}")
                        
        except asyncio.CancelledError:
            logger.info("Autonomous Scheduler kapatılıyor (FastAPI Shutdown)...")
            break
        except Exception as e:
            logger.error(f"Autonomous Scheduler Çöktü: {e}")
        
        # Bekle (Varsayılan 14 Dakika / 840 saniye)
        logger.info(f"Autonomous Scheduler: Tarama bitti. Ukuya geçiliyor ({INTERVAL_SECONDS // 60} Dakika).")
        await asyncio.sleep(INTERVAL_SECONDS)
