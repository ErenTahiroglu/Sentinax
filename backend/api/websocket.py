"""
📡 WebSocket Gerçek Zamanlı Fiyat Akışı — P3 Öncelik Matrisi
=============================================================
Polygon.io WebSocket proxy endpoint.
• POLYGON_API_KEY env var yoksa endpoint kayıt edilmez (graceful).
• Birden fazla client aynı WS bağlantısını paylaşır (Fan-out pattern).
• Ticker listesi client tarafından gönderilir.
"""
import asyncio
import json
import logging
import os
from typing import Set, Optional

logger = logging.getLogger(__name__)

POLYGON_API_KEY: Optional[str] = os.getenv("POLYGON_API_KEY")
_POLYGON_WS_URL = "wss://delayed.polygon.io/stocks"

# Aktif WebSocket client seti (broadcast için)
_clients: Set = set()
_polygon_connected = False


async def _polygon_reader(send_fn):
    """Polygon.io'ya bağlanır ve gelen fiyat tick'lerini broadcast eder."""
    global _polygon_connected
    import websockets  # lazy import

    retry_delay = 1
    while True:
        try:
            async with websockets.connect(_POLYGON_WS_URL) as ws:
                retry_delay = 1 # Reset on success
                await ws.send(json.dumps({"action": "auth", "params": POLYGON_API_KEY}))
                _polygon_connected = True
                logger.info("✅ Polygon.io WebSocket bağlantısı kuruldu.")

                async for raw in ws:
                    messages = json.loads(raw)
                    for msg in messages:
                        ev = msg.get("ev")
                        if ev in ("T", "Q"):  # Trade / Quote events
                            await send_fn(json.dumps(msg))
        except Exception as e:
            _polygon_connected = False
            logger.warning(f"Polygon WS Upstream bağlantısı kesildi: {e}. {retry_delay}s sonra yeniden bağlanmaya çalışılacak.")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60) # Max 1 m backoff


def register_websocket_routes(app, router=None):
    """FastAPI app veya router'a WebSocket endpoint ekler.
    POLYGON_API_KEY yoksa hiçbir şey yapmaz."""
    if not POLYGON_API_KEY:
        logger.info(
            "ℹ️ POLYGON_API_KEY tanımlı değil — WebSocket endpoint devre dışı."
        )
        return

    target = router or app
    from fastapi import WebSocket, WebSocketDisconnect

    @target.websocket("/ws/prices")
    async def prices_ws(websocket: WebSocket, token: Optional[str] = None):
        """
        Gerçek zamanlı fiyat stream endpoint'i.
        • 🛡️ CSWSH / DoW Koruması: Token validation REQUIRED before accept.
        """
        if not token:
             logger.warning("WebSocket: Connection attempt without token.")
             await websocket.close(code=1008) # Policy Violation
             return

        # Token Doğrulama
        from fastapi.concurrency import run_in_threadpool
        from backend.infrastructure.auth import verify_token_string
        try:
             # Threadpool: a JWKS refresh is blocking network I/O and must not stall the event loop.
             await run_in_threadpool(verify_token_string, token)
        except Exception as e:
             logger.warning(f"WebSocket: Invalid token connection attempt -> {e}")
             await websocket.close(code=1008)
             return

        await websocket.accept()
        _clients.add(websocket)
        logger.info(f"🔌 WebSocket client bağlandı. Toplam: {len(_clients)}")

        # Polygon fan-out broadcaster
        async def broadcast(data: str):
            dead = set()
            for client in _clients.copy():
                try:
                    await client.send_text(data)
                except Exception:
                    dead.add(client)
            _clients.difference_update(dead)

        # İlk bağlantıda Polygon reader'ı başlat (singleton)
        if not _polygon_connected:
            asyncio.create_task(_polygon_reader(broadcast))

        try:
            while True:
                raw = await websocket.receive_text()
                msg = json.loads(raw)
                if msg.get("action") == "subscribe" and POLYGON_API_KEY:
                    tickers = ",".join(msg.get("tickers", []))
                    logger.info(f"📈 Ticker aboneliği: {tickers}")
        except WebSocketDisconnect:
            _clients.discard(websocket)
            logger.info(f"🔌 WebSocket client ayrıldı. Kalan: {len(_clients)}")
