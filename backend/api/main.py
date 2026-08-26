"""
🧩 Puzzle Parça: API Giriş Noktası (Entrypoint) — v4.0
======================================================
FastAPI uygulamasını başlatır, middleware'leri tanımlar ve
dekuple edilmiş router'ları include eder.
"""

import os
import logging
import asyncio
import sys
from typing import cast
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.exceptions import RequestValidationError
from starlette.middleware.base import BaseHTTPMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

from backend.infrastructure.scheduler import start_alert_scheduler
from backend.infrastructure.redis_cache import cache_close, cache_get, cache_set, cache_delete, cache_is_redis_active
from backend.infrastructure.http_client import init_global_http_client, close_global_http_client
from backend.api.websocket import register_websocket_routes, _clients
from backend.utils.logger import setup_logging, CorrelationIdMiddleware, correlation_id_ctx
from backend.api.routers import analysis, chat, user, admin, billing, telemetry, buffett

def validate_critical_env():
    """Kritik ortam değişkenlerini başlatmadan önce doğrular (Fail-Fast)."""
    if "pytest" in sys.modules:
        return # Test çalışırken validation atla
        
    critical_vars = ["SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"]
    missing = [v for v in critical_vars if not os.getenv(v)]
    if missing:
        # 🛡️ Gevşetilmiş hata (Sistemin kalkmasına izin verir, ama log'a basar)
        print(f"⚠️ UYARI: Kritik ortam değişkenleri eksik: {', '.join(missing)}")
        print("Sistem işlevleri (Örn: DB, Auth) kısıtlı çalışabilir.")

# ── Logging Setup ─────────────────────────────────────────────────────────
setup_logging()
logger = logging.getLogger(__name__)

validate_critical_env()


# ── Lifespan (Background Tasks) ───────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # API kalktığında HTTP Client havuzunu başlat
    init_global_http_client()
    
    # API kalktığında otonom tarayıcıyı ayağa kaldır
    task = asyncio.create_task(start_alert_scheduler())
    yield
    # API kapandığında memory leak olmaması için iptal et
    task.cancel()
    # Redis ve HTTP havuzunu kapat (Graceful Shutdown)
    try:
        await close_global_http_client()
        cache_close()
        logger.info("✅ Redis & HTTP Sessions safely closed.")
    except Exception as e:
         logger.warning(f"Error during shutdown closures: {e}")

# ── FastAPI Uygulaması ────────────────────────────────────────────────────
is_prod = os.getenv("ENVIRONMENT", "production").lower() == "production"

app = FastAPI(
    title="Portföy Analiz Platformu", 
    version="4.0", 
    lifespan=lifespan,
    docs_url=None if is_prod else "/docs",
    redoc_url=None if is_prod else "/redoc"
)

# ── Metrics (Prometheus) ──────────────────────────────────────────────────
Instrumentator().instrument(app).expose(app, endpoint="/metrics")

# ── Middleware: Idempotency ───────────────────────────────────────────────
class IdempotencyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.method not in ["POST", "PUT", "PATCH"]:
            return await call_next(request)
            
        idempotency_key = request.headers.get("Idempotency-Key")
        if not idempotency_key:
            return await call_next(request)
            
        redis_key = f"idemp:{idempotency_key}"
        cached = cache_get(redis_key)
        
        if cached:
            if cached.get("status") == "PROCESSING":
                return JSONResponse(
                    content={"error": True, "message": "İşlem arka planda devam ediyor (Zaten işleniyor)."},
                    status_code=409,
                    headers={"Idempotency-Key": idempotency_key}
                )
            elif cached.get("status") == "COMPLETED":
                headers = cached.get("headers", {})
                headers["Idempotency-Key"] = idempotency_key
                headers["X-Idempotency-Cache"] = "HIT"
                return Response(
                    content=cached.get("response_body", "{}").encode("utf-8"),
                    status_code=cached.get("status_code", 200),
                    headers=headers,
                    media_type=headers.get("content-type", "application/json")
                )
        
        # İşlemi Kilitli Olarak İşaretle (PROCESSING) - Max 5 dakika
        cache_set(redis_key, {"status": "PROCESSING"}, ttl=300)
        
        try:
            response = await call_next(request)
            
            content_type = response.headers.get("content-type", "")
            
            # SSE akışlarını (StreamingResponse) belleğe almamak için kilidi kaldırırız
            # Böylece yeniden denendiğinde güvenle baştan çalıştırılabilir.
            if "text/event-stream" in content_type:
                cache_delete(redis_key)
                return response
                
            # Standart JSON yanıtlarını cache'leriz
            if "application/json" in content_type:
                res_body = b""
                if isinstance(response, StreamingResponse):
                    async for chunk in response.body_iterator:
                        res_body += cast(bytes, chunk)
                else:
                    # Non-streaming responses like JSONResponse already have .body
                    res_body = cast(bytes, getattr(response, "body", b""))
                
                new_response = Response(
                    content=res_body,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    media_type=response.media_type
                )
                
                # Sadece başarılı istekleri cache'le
                if response.status_code < 400:
                    cache_set(redis_key, {
                        "status": "COMPLETED",
                        "response_body": res_body.decode("utf-8", errors="ignore"),
                        "headers": dict(response.headers),
                        "status_code": response.status_code
                    }, ttl=300)
                else:
                    cache_delete(redis_key)
                    
                return new_response
            
            cache_delete(redis_key)
            return response
            
        except Exception as e:
            cache_delete(redis_key)
            raise e

# ── Middleware: No-Cache ──────────────────────────────────────────────────
class NoCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/ui"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

app.add_middleware(CorrelationIdMiddleware)
app.add_middleware(IdempotencyMiddleware)
app.add_middleware(NoCacheMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=1000)


# ── Middleware: CORS ──────────────────────────────────────────────────────
origins_str = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000,http://localhost:5500,http://127.0.0.1:5500,https://ai-destekli-portfoy-yoneticisi.vercel.app")
origins = [o.strip() for o in origins_str.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*", "X-Correlation-ID"],
    expose_headers=["X-Correlation-ID"],
)

# ── Exception Handlers ──────────────────────────────────────────────────
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    corr_id = correlation_id_ctx.get()
    headers = getattr(exc, "headers", {}) or {}
    
    # 🛡️ Rate Limit (429) için Retry-After ekle
    if exc.status_code == 429 and "Retry-After" not in headers:
        headers["Retry-After"] = "60" # Varsayılan 1 dakika

    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": True,
            "message": exc.detail,
            "status_code": exc.status_code,
            "correlation_id": corr_id
        },
        headers=headers
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    corr_id = correlation_id_ctx.get()
    return JSONResponse(
        status_code=422,
        content={
            "error": True,
            "message": "Veri doğrulama hatası (Validation Error)",
            "detail": exc.errors(),
            "status_code": 422,
            "correlation_id": corr_id
        }
    )

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    corr_id = correlation_id_ctx.get()
    logger.error(f"Unhandled Exception: {str(exc)}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": True,
            "message": "Sunucu tarafında beklenmedik bir hata oluştu.",
            "status_code": 500,
            "correlation_id": corr_id
        }
    )

# ── WebSocket Entegrasyonu ────────────────────────────────────────────────
register_websocket_routes(app)

# ── Sub-Routers Include ───────────────────────────────────────────────────
# app.include_router(analysis.router)
# app.include_router(chat.router)
# app.include_router(user.router)
# app.include_router(admin.router)
# app.include_router(billing.router)
# app.include_router(telemetry.router)

# ── Buffett Engine Router ─────────────────────────────────────────────────
app.include_router(buffett.router)

# ── Root Redirect & Static Files (Frontend) ───────────────────────────────
@app.get("/api/health")
async def health_check():
    """
    Sistem Sağlık Kontrolü (Liveness Probe).
    🛡️ Render/K8s çökme döngüsünü engeller, DB & Redis denetler.
    """
    health_status = {"status": "ok", "redis": "connected", "supabase": "connected"}
    
    # 1. Redis Check
    if not cache_is_redis_active():
        health_status["redis"] = "disconnected (fallback active)"

    # 2. Supabase Check
    try:
        import httpx
        from backend.infrastructure.scheduler import SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
        headers = {
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}"
        }
        async with httpx.AsyncClient(timeout=3.0) as client:
            # Basit bir limit=1 query atarak bağlantıyı test edelim
            resp = await client.get(f"{SUPABASE_URL}/rest/v1/portfolios?limit=1", headers=headers)
            if resp.status_code not in (200, 206):
                health_status["supabase"] = f"error (HTTP {resp.status_code})"
                health_status["status"] = "degraded"
    except Exception as e:
         health_status["supabase"] = f"error: {str(e)}"
         health_status["status"] = "critical"

    return health_status

@app.get("/api/metrics")
async def get_metrics():
    """
    📊 Uygulama Telemetri Verileri (Monitoring).
    🛡️ Anlık aktif WebSocket bağlantı sayılarını ve sağlık özetini döner.
    """
    from datetime import datetime, timezone
    return {
        "active_websocket_connections": len(_clients),
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.get("/")
def read_root():
    return RedirectResponse(url="/buffett_ui")

base_dir = os.path.dirname(os.path.abspath(__file__))
# In Monorepo, /frontend is at the root, 2 levels up from backend/api/
frontend_path = os.path.join(os.path.dirname(os.path.dirname(base_dir)), "frontend")
buffett_path = os.path.join(frontend_path, "buffett")

if os.path.exists(buffett_path):
    app.mount("/buffett_ui", StaticFiles(directory=buffett_path, html=True), name="buffett_ui")
else:
    logger.warning(f"Buffett UI Static path not found at {buffett_path}")
