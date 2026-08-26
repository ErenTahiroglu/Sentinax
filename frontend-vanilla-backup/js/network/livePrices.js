/**
 * 📡 Gerçek Zamanlı Fiyat Takibi — P3 Öncelik Matrisi
 * ======================================================
 * WebSocket /ws/prices endpoint'ine bağlanır.
 * Polygon.io'dan gelen fiyat tick'lerini dinler ve
 * kart DOM'larını flash animasyonla günceller.
 *
 * Kullanım:
 *   import { initLivePrices, subscribeTickers } from './network/livePrices.js';
 *   initLivePrices();
 *   subscribeTickers(['AAPL', 'TSLA', 'GOOGL']);
 */

/** @type {WebSocket|null} */
let _socket = null;
let _reconnectTimer = null;
let _subscribedTickers = new Set();

/** Son bilinen fiyatlar: { "AAPL": 182.5, ... } */
const _priceCache = {};

const WS_URL = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/prices`;

/**
 * WebSocket bağlantısını başlatır.
 * Bağlantı başarısız olursa 5s sonra yeniden dener.
 */
export function initLivePrices() {
    if (_socket && _socket.readyState < 2) return; // Zaten bağlı veya bağlanıyor

    console.info('[LivePrices] WebSocket başlatılıyor…');
    try {
        _socket = new WebSocket(WS_URL);
    } catch (e) {
        console.warn('[LivePrices] WebSocket başlatılamadı — polling moduna geç:', e);
        return;
    }

    _socket.onopen = () => {
        console.info('[LivePrices] ✅ Bağlantı kuruldu');
        clearTimeout(_reconnectTimer);
        // Önceki abonelikleri yeniden gönder
        if (_subscribedTickers.size > 0) {
            _sendSubscribe([..._subscribedTickers]);
        }
    };

    _socket.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);
            _handleTick(msg);
        } catch (e) {
            // Sessizce görmezden gel
        }
    };

    _socket.onclose = () => {
        // 🛡️ Thundering Herd (Sürü Fırtınası) Koruması: Jitter (Rastgele Milisaniye Sapması)
        const jitter = Math.random() * 2000; 
        console.warn(`[LivePrices] ⚠️ Bağlantı kapandı — ${(5000+jitter)/1000}s sonra yeniden deneniyor`);
        _reconnectTimer = setTimeout(initLivePrices, 5000 + jitter);
    };

    _socket.onerror = (e) => {
        console.warn('[LivePrices] Bağlantı hatası — WebSocket devre dışı:', e);
        _socket?.close();
    };
}

/**
 * Takip edilecek ticker'ları günceller.
 * @param {string[]} tickers - Büyük harfli ticker sembol listesi ['AAPL', ...]
 */
export function subscribeTickers(tickers) {
    _subscribedTickers = new Set(tickers);
    if (_socket?.readyState === WebSocket.OPEN) {
        _sendSubscribe(tickers);
    }
}

function _sendSubscribe(tickers) {
    _socket?.send(JSON.stringify({ action: 'subscribe', tickers }));
}

/**
 * Polygon.io Trade tick'ini işler.
 * @param {{ ev: string, sym: string, p: number, s: number }} msg
 */
function _handleTick(msg) {
    if (msg.ev !== 'T') return; // Sadece Trade event'leri işle

    // 🛡️ SRE Stale State Tracking
    window.lastPricesUpdatedAt = Date.now();

    const ticker = msg.sym;
    const price  = msg.p;   // Price
    const prev   = _priceCache[ticker];

    _priceCache[ticker] = price;
    _updateCard(ticker, price, prev);
}

/**
 * Kart DOM'undaki fiyat elementini günceller ve flash animasyonu tetikler.
 * @param {string} ticker
 * @param {number} price
 * @param {number|undefined} prevPrice
 */
function _updateCard(ticker, price, prevPrice) {
    // Fiyat elementini bul: data-live-price="AAPL" attribute'u ile
    const elements = document.querySelectorAll(`[data-live-price="${ticker}"]`);
    if (!elements.length) return;

    const direction = prevPrice !== undefined
        ? price > prevPrice ? 'up' : price < prevPrice ? 'down' : 'neutral'
        : 'neutral';

    elements.forEach(el => {
        el.textContent = `$${price.toFixed(2)}`;
        el.classList.remove('price-flash-up', 'price-flash-down');
        // Reflow zorla (animasyonun yeniden başlaması için)
        void el.offsetWidth;
        if (direction !== 'neutral') {
            el.classList.add(`price-flash-${direction}`);
        }
    });
}

/**
 * Son bilinen fiyatı döndürür (offline/polling modu için).
 * @param {string} ticker
 * @returns {number|null}
 */
export function getLastPrice(ticker) {
    return _priceCache[ticker] ?? null;
}
