import logging
import os
import httpx
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Depends, Request, Header
from typing import Optional
from backend.api.models import UserSettingsRequest, OnboardingProfileRequest
from backend.infrastructure.auth import verify_jwt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["User"])


# ════════════════════════════════════════════════════
# ONBOARDING PROFILE ENDPOINTS
# ════════════════════════════════════════════════════

@router.get("/onboarding", dependencies=[Depends(verify_jwt)])
async def get_onboarding_profile(request: Request):
    """
    Kullanıcının onboarding profili ve tamamlama bayrağını döndürür.
    Frontend bu endpoint'i uygulama yüklenirken sihirbazı gösterip göstermeyeceğine
    karar vermek için kullanır.
    """
    user = getattr(request.state, "user", None)
    if not user or "sub" not in user:
        raise HTTPException(status_code=401, detail="User Identity Not Found")

    user_id = user["sub"]
    supa_url = os.getenv("SUPABASE_URL", "")
    supa_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

    url = f"{supa_url}/rest/v1/user_settings?user_id=eq.{user_id}&select=onboarding_profile,is_onboarded"
    headers = {"apikey": supa_key, "Authorization": f"Bearer {supa_key}"}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                if data:
                    return {
                        "is_onboarded": data[0].get("is_onboarded", False),
                        "onboarding_profile": data[0].get("onboarding_profile"),
                    }
        return {"is_onboarded": False, "onboarding_profile": None}
    except Exception as e:
        logger.error(f"Onboarding profile fetch failed: {e}")
        return {"is_onboarded": False, "onboarding_profile": None}


@router.post("/onboarding", dependencies=[Depends(verify_jwt)])
async def save_onboarding_profile(body: OnboardingProfileRequest, request: Request):
    """
    Kullanıcının onboarding seçimlerini user_settings tablosuna ükler/günceller.
    is_onboarded bayrağını TRUE olarak işaretler.
    """
    user = getattr(request.state, "user", None)
    if not user or "sub" not in user:
        raise HTTPException(status_code=401, detail="User Identity Not Found")

    user_id = user["sub"]
    supa_url = os.getenv("SUPABASE_URL", "")
    supa_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

    profile_payload = {
        "level": body.level,
        "goal": body.goal,
        "riskTolerance": body.risk_tolerance,
    }

    upsert_body = {
        "user_id": user_id,
        "onboarding_profile": profile_payload,
        "is_onboarded": True,
    }

    url = f"{supa_url}/rest/v1/user_settings"
    headers = {
        "apikey": supa_key,
        "Authorization": f"Bearer {supa_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }

    try:
        if request.headers.get("x-shadow-test", "").lower() == "true":
            logger.info(f"🛡️ Shadow bypass triggered. Skipping DB POST to {url}")
            return {"status": "success", "shadow_bypassed": True}
            
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, headers=headers, json=upsert_body)
            if resp.status_code not in (200, 201):
                raise HTTPException(status_code=500, detail=f"DB Error: {resp.text}")
        return {"status": "success"}
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        logger.error(f"Onboarding profile save failed: {e}")
        raise HTTPException(status_code=500, detail=f"DB Error: {e}")



@router.get("/alerts", dependencies=[Depends(verify_jwt)])
async def get_alerts(request: Request):
    """Kullanıcının Supabase alerts tablosundaki okunmamış son 10 bildirimini getir."""
    user = getattr(request.state, "user", None)
    if not user or "sub" not in user:
        raise HTTPException(status_code=401, detail="User Identity Not Found")

    user_id = user["sub"]
    supa_url = os.getenv('SUPABASE_URL', '')
    supa_key = os.getenv('SUPABASE_SERVICE_ROLE_KEY', '')
    
    url = f"{supa_url}/rest/v1/alerts?user_id=eq.{user_id}&is_read=eq.false&order=created_at.desc&limit=10"
    headers = {
        "apikey": supa_key,
        "Authorization": f"Bearer {supa_key}"
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
        return []
    except Exception as e:
        logger.error(f"Alerts fetch failed: {e}")
        return []

@router.post("/alerts/read", dependencies=[Depends(verify_jwt)])
async def mark_alerts_read(request: Request):
    """Kullanıcının tüm okunmamış bildirimlerini okundu olarak işaretler."""
    user = getattr(request.state, "user", None)
    if not user or "sub" not in user:
        raise HTTPException(status_code=401, detail="User Identity Not Found")

    user_id = user["sub"]
    supa_url = os.getenv('SUPABASE_URL', '')
    supa_key = os.getenv('SUPABASE_SERVICE_ROLE_KEY', '')
    
    url = f"{supa_url}/rest/v1/alerts?user_id=eq.{user_id}&is_read=eq.false"
    headers = {
        "apikey": supa_key,
        "Authorization": f"Bearer {supa_key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
    }
    payload = {"is_read": True}

    try:
        if request.headers.get("x-shadow-test", "").lower() == "true":
            logger.info("🛡️ Shadow bypass triggered for alerts patch.")
            return {"status": "success", "shadow_bypassed": True}
            
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.patch(url, headers=headers, json=payload)
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Mark alerts read failed: {e}")
        raise HTTPException(status_code=500, detail=f"DB Error: {e}")

@router.get("/user-settings", dependencies=[Depends(verify_jwt)])
async def get_user_settings(request: Request):
    user = getattr(request.state, "user", None)
    if not user or "sub" not in user:
        raise HTTPException(status_code=401, detail="User Identity Not Found")

    user_id = user["sub"]
    supa_url = os.getenv('SUPABASE_URL', '')
    supa_key = os.getenv('SUPABASE_SERVICE_ROLE_KEY', '')
    
    url = f"{supa_url}/rest/v1/user_settings?user_id=eq.{user_id}"
    headers = { "apikey": supa_key, "Authorization": f"Bearer {supa_key}" }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                if data:
                    return data[0]
        return {"telegram_chat_id": "", "risk_tolerance": "Orta", "commission_rate": 0.002, "slippage_rate": 0.001}
    except Exception as e:
        logger.error(f"User settings fetch failed: {e}")
        return {"telegram_chat_id": "", "risk_tolerance": "Orta", "commission_rate": 0.002, "slippage_rate": 0.001}

@router.post("/user-settings", dependencies=[Depends(verify_jwt)])
async def update_user_settings(settings: UserSettingsRequest, request: Request):
    user = getattr(request.state, "user", None)
    if not user or "sub" not in user:
        raise HTTPException(status_code=401, detail="User Identity Not Found")

    user_id = user["sub"]
    supa_url = os.getenv('SUPABASE_URL', '')
    supa_key = os.getenv('SUPABASE_SERVICE_ROLE_KEY', '')
    
    url = f"{supa_url}/rest/v1/user_settings"
    headers = {
        "apikey": supa_key,
        "Authorization": f"Bearer {supa_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates"
    }
    payload = {
        "user_id": user_id,
        "telegram_chat_id": settings.telegram_chat_id,
        "risk_tolerance": settings.risk_tolerance,
        "commission_rate": settings.commission_rate,
        "slippage_rate": settings.slippage_rate
    }

    try:
        if request.headers.get("x-shadow-test", "").lower() == "true":
            logger.info("🛡️ Shadow bypass triggered for user_settings.")
            return {"status": "success", "shadow_bypassed": True}
            
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code not in [200, 201]:
                raise HTTPException(status_code=500, detail=f"DB Error: {resp.text}")
        return {"status": "success"}
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        logger.error(f"User settings update failed: {e}")
        raise HTTPException(status_code=500, detail=f"DB Error: {e}")

@router.get("/paper-trades", dependencies=[Depends(verify_jwt)])
async def get_paper_trades(request: Request):
    user = getattr(request.state, "user", None)
    if not user or "sub" not in user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    user_id = user["sub"]
    supa_url = os.getenv('SUPABASE_URL', '')
    supa_key = os.getenv('SUPABASE_SERVICE_ROLE_KEY', '')
    if not supa_url or not supa_key:
        return []

    url = f"{supa_url}/rest/v1/paper_trades?user_id=eq.{user_id}&order=timestamp.desc&limit=50"
    headers = { "apikey": supa_key, "Authorization": f"Bearer {supa_key}" }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
        return []
    except Exception as e:
        logger.error(f"Paper trades fetch failed: {e}")
        return []

@router.get("/portfolio/history", dependencies=[Depends(verify_jwt)])
async def get_portfolio_history(request: Request):
    """Kullanıcının geçmiş portföy snapshot verilerini (son 30 gün) getirir."""
    user = getattr(request.state, "user", None)
    if not user or "sub" not in user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    user_id = user["sub"]
    supa_url = os.getenv('SUPABASE_URL', '')
    supa_key = os.getenv('SUPABASE_SERVICE_ROLE_KEY', '')
    if not supa_url or not supa_key:
        return []

    url = f"{supa_url}/rest/v1/portfolio_snapshots?user_id=eq.{user_id}&order=timestamp.desc&limit=30"
    headers = { "apikey": supa_key, "Authorization": f"Bearer {supa_key}" }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200: 
                data = resp.json()
                return data[::-1]  # Kronolojik sıra (eskiden yeniye)
        return []
    except Exception as e:
        logger.error(f"Portfolio history fetch failed: {e}")
        return []

@router.post("/logout", dependencies=[Depends(verify_jwt)])
async def logout(request: Request):
    """
    Kullanıcı oturumunu kapatır ve token'ı Redis Blocklist'e ekler.
    🛡️ Session Hijacking prevent.
    """
    authorization = request.headers.get("Authorization")
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ")[1]
        try:
             from backend.infrastructure.redis_cache import cache_set
             # Token'ı 24 Saatliğine kara listeye al (Max TTL simülasyonu)
             cache_set(f"jwt_blacklist:{token}", {"revoked": True}, ttl=86400)
             logger.info("User JWT blacklisted successfully on logout.")
        except ImportError:
             logger.warning("Redis support missing, token not listed.")
             
@router.get("/export-data", dependencies=[Depends(verify_jwt)])
async def export_data(request: Request):
    """
    Kullanıcının sistemdeki tüm verilerini (KVKK/GDPR Veri Taşınabilirliği) JSON formatında çıktılar.
    """
    user_id = request.state.user.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="User context required.")

    results = {
        "user_id": user_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": {}
    }

    try:
        import httpx
        from backend.infrastructure.scheduler import SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
        
        headers = {
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}"
        }

        async with httpx.AsyncClient() as client:
            # 1. Portfolios
            p_resp = await client.get(f"{SUPABASE_URL}/rest/v1/portfolios?user_id=eq.{user_id}", headers=headers)
            results["data"]["portfolios"] = p_resp.json() if p_resp.status_code == 200 else []

            # 2. Paper Trades
            t_resp = await client.get(f"{SUPABASE_URL}/rest/v1/paper_trades?user_id=eq.{user_id}", headers=headers)
            results["data"]["paper_trades"] = t_resp.json() if t_resp.status_code == 200 else []

            # 3. Alerts
            a_resp = await client.get(f"{SUPABASE_URL}/rest/v1/alerts?user_id=eq.{user_id}", headers=headers)
            results["data"]["alerts"] = a_resp.json() if a_resp.status_code == 200 else []

            # 4. User Settings
            s_resp = await client.get(f"{SUPABASE_URL}/rest/v1/user_settings?user_id=eq.{user_id}", headers=headers)
            results["data"]["user_settings"] = s_resp.json() if s_resp.status_code == 200 else []

            # 5. Portfolio Snapshots
            snap_resp = await client.get(f"{SUPABASE_URL}/rest/v1/portfolio_snapshots?user_id=eq.{user_id}", headers=headers)
            results["data"]["portfolio_snapshots"] = snap_resp.json() if snap_resp.status_code == 200 else []

            # 6. LLM Usage Logs
            llm_resp = await client.get(f"{SUPABASE_URL}/rest/v1/llm_usage_logs?user_id=eq.{user_id}", headers=headers)
            results["data"]["llm_usage_logs"] = llm_resp.json() if llm_resp.status_code == 200 else []

    except Exception as e:
         logger.error(f"GDPR Export failed ({user_id}): {e}")
         raise HTTPException(status_code=500, detail="Data export failed. Please try again later.")

    return results

@router.get("/portfolio", dependencies=[Depends(verify_jwt)])
async def get_portfolio(request: Request):
    """Kullanıcının güncel portföy dökümünü (tickers) getirir."""
    user_id = request.state.user.get("sub")
    if not user_id: 
        raise HTTPException(status_code=401, detail="Unauthorized")

    supa_url = os.getenv('SUPABASE_URL', '')
    supa_key = os.getenv('SUPABASE_SERVICE_ROLE_KEY', '')
    if not supa_url or not supa_key:
        return {"tickers": []}

    url = f"{supa_url}/rest/v1/portfolios?user_id=eq.{user_id}"
    headers = { "apikey": supa_key, "Authorization": f"Bearer {supa_key}" }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                if data:
                    return data[0]
        return {"tickers": []}
    except Exception as e:
        logger.error(f"Portfolio fetch failed: {e}")
        return {"tickers": []}

@router.post("/portfolio", dependencies=[Depends(verify_jwt)])
async def save_portfolio(request: Request, x_shadow_test: Optional[str] = Header(default=None)):
    """Kullanıcının portföyünü (tickers) güvenli bir şekilde kaydeder/günceller."""
    user_id = request.state.user.get("sub")
    if not user_id: 
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        payload = await request.json()
        if "tickers" not in payload:
            raise HTTPException(status_code=400, detail="Missing tickers")

        supa_url = os.getenv('SUPABASE_URL', '')
        supa_key = os.getenv('SUPABASE_SERVICE_ROLE_KEY', '')
        if not supa_url or not supa_key:
            raise HTTPException(status_code=500, detail="DB Config Missing")

        # 🛡️ PostgREST Upsert (Conflict on user_id)
        body = {
            "user_id": user_id,
            "tickers": payload["tickers"],
            "updated_at": datetime.now(timezone.utc).isoformat()
        }

        # 🛡️ PostgREST Upsert (Conflict on user_id)
        # Prefer: resolution=merge-duplicates veya direct upsert can be used.
        # Alternatif: POST with resolution=merge-duplicates
        headers = {
            "apikey": supa_key,
            "Authorization": f"Bearer {supa_key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates" 
        }

        is_shadow = (x_shadow_test.lower() == 'true') if x_shadow_test else False
        if is_shadow:
            logger.info("🛡️ Shadow bypass triggered for portfolio save.")
            return {"status": "success", "shadow_bypassed": True}

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(f"{supa_url}/rest/v1/portfolios", json=body, headers=headers)
            if resp.status_code in (200, 201):
                return {"status": "success"}
            else:
                logger.error(f"Portfolio upsert error: {resp.status_code} - {resp.text}")
                # Fallback to PATCH if merge fails or unsupported
                patch_url = f"{supa_url}/rest/v1/portfolios?user_id=eq.{user_id}"
                patch_headers = headers.copy()
                patch_headers["Prefer"] = "return=minimal"
                patch_resp = await client.patch(patch_url, json={"tickers": payload["tickers"], "updated_at": body["updated_at"]}, headers=patch_headers)
                if patch_resp.status_code in (200, 204):
                    return {"status": "success"}
                raise HTTPException(status_code=500, detail="Database write failed")
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=400, detail=str(e))
