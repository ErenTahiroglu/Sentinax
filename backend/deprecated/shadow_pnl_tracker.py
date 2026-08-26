import os
import httpx
import asyncio
import logging
from decimal import Decimal, getcontext, ROUND_HALF_UP
from datetime import datetime, timezone
import yfinance as yf

# 🛡️ Financial Precision Setup (Standard for banking/trading)
getcontext().prec = 28
getcontext().rounding = ROUND_HALF_UP

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("ShadowPnLTracker")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

async def update_missing_pnl():
    """
    Global PnL güncelleyici. user_settings tablosundaki bireysel oranları baz alarak
    shadow_divergence_logs sonuçlarını hesaplar.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.error("Eksik Supabase yapılandırması!")
        return
        
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    }
    
    async with httpx.AsyncClient() as client:
        # Son 50 kaydı çek
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/shadow_divergence_logs?select=*&order=created_at.desc&limit=50",
            headers=headers
        )
        if resp.status_code != 200:
            logger.error(f"Kayıtlar çekilemedi: {resp.status_code}")
            return
            
        logs = resp.json()
        now = datetime.now(timezone.utc)
        
        for log in logs:
            created_at = datetime.fromisoformat(log["created_at"].replace("Z", "+00:00"))
            days_passed = (now - created_at).days
            
            ticker = log["ticker"]
            t0 = Decimal(str(log.get("t0_price", "1.0")))
            user_id = log.get("user_id") # Null ise global default kullanılır
            
            # Kullanıcıya özel oranları çek (veya varsayılan %0.2 / %0.1)
            comm_rate, slip_rate = await _get_user_rates(client, headers, user_id)
            
            updates = {}
            for period in [1, 3, 7]:
                col_price = f"t{period}_price"
                col_winner = f"winner_t{period}"
                
                if days_passed >= period and log.get(col_price) is None:
                    tn, winner = await evaluate_pnl_dynamic(
                        ticker, t0, log["old_decision"], log["new_decision"],
                        comm_rate, slip_rate
                    )
                    if tn is not None:
                        updates[col_price] = float(tn)
                        updates[col_winner] = winner
            
            if updates:
                await client.patch(
                    f"{SUPABASE_URL}/rest/v1/shadow_divergence_logs?id=eq.{log['id']}",
                    headers=headers,
                    json=updates
                )
                logger.info(f"[{ticker}] PnL güncellendi (Kullanıcı: {user_id}): {updates}")

async def _get_user_rates(client, headers, user_id):
    """Kullanıcının veritabanındaki komisyon ve kayma oranlarını döner."""
    if not user_id:
        return Decimal("0.002"), Decimal("0.001")
        
    try:
        url = f"{SUPABASE_URL}/rest/v1/user_settings?user_id=eq.{user_id}&select=commission_rate,slippage_rate"
        resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            if data:
                return (
                    Decimal(str(data[0].get("commission_rate", "0.002"))),
                    Decimal(str(data[0].get("slippage_rate", "0.001")))
                )
    except Exception as e:
        logger.warning(f"Kullanıcı oranları çekilemedi ({user_id}): {e}")
        
    return Decimal("0.002"), Decimal("0.001")

async def evaluate_pnl_dynamic(ticker, t0, old_dec, new_dec, comm_rate, slip_rate):
    """
    Dinamik maliyetlerle (Decimal) PnL hesaplar.
    Logic:
    - BUY: Net = (tn - t0)/t0 - (Entry_Cost + Exit_Cost)
    - SELL/HOLD: Ayrıştırılmış maliyet kalkanı.
    """
    try:
        data = yf.Ticker(ticker)
        await asyncio.sleep(0.5) 
        hist = data.history(period="1d")
        if hist.empty:
            return None, None
            
        tn = Decimal(str(hist["Close"].iloc[-1]))
        
        # Sürtünme Maliyeti (Friction) = Komisyon + Kayma
        friction = comm_rate + slip_rate
        
        # 1. 'HOLD' ve 'SELL' Farkını Modelle
        # SELL sinyali bir 'ÇIKIŞ' maliyeti üretir.
        # HOLD ise mevcut pozisyonda beklemedir (yeni maliyet yok).
        
        def calc_perf(decision, price_start, price_end):
            # Karar BUY ise: Fiyat artışı bekliyoruz, giriş-çıkış maliyeti düşer.
            if "BUY" in decision:
                gross = (price_end - price_start) / price_start
                return gross - (Decimal("2.0") * friction) # Alırken ve satarken
            
            # Karar SELL ise: Fiyat düşüşünden kaçmak istiyoruz.
            # Bir kereye mahsus 'ÇIKIŞ' maliyeti öderiz.
            if "SELL" in decision:
                # Potansiyel kalkan (Shield): Eğer düşüş beklentisi maliyetten azsa, HOLD daha iyidir.
                avoided_loss = (price_start - price_end) / price_start
                return avoided_loss - friction
                
            # Karar HOLD ise: Bekliyoruz. Mevcut fiyat hareketini olduğu gibi alırız.
            # Eğer nakitte beklemek anlamındaysa 0.0 olur, ancak biz 'Varlığı Tut' olarak modelliyoruz.
            return (price_end - price_start) / price_start

        old_perf = calc_perf(old_dec, t0, tn)
        new_perf = calc_perf(new_dec, t0, tn)
        
        # 🛡️ Profit-Cost Shield: 
        # Eğer yeni karar (SELL) maliyet yüzünden negatif performans üretiyorsa 
        # ve HOLD (karar vermeme) daha iyiyse, sistem mantıksal olarak HOLD'u tercih etmeli.
        # (Not: Bu mantık karar alma aşamasında kullanılır, burada sadece performans ölçüyoruz.)

        winner = "TIE"
        if new_perf > old_perf:
            winner = "NEW"
        elif old_perf > new_perf:
            winner = "OLD"
            
        return tn, winner
    except Exception as e:
        logger.warning(f"PnL evaluation failed for {ticker}: {e}")
        return None, None

async def _evaluate_pnl(ticker, t0, old_dec, new_dec):
    """Legacy wrapper for backward compatibility with older tests."""
    return await evaluate_pnl_dynamic(
        ticker, Decimal(str(t0)), old_dec, new_dec, 
        Decimal("0.002"), Decimal("0.001")
    )

if __name__ == "__main__":
    asyncio.run(update_missing_pnl())
