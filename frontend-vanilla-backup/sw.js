const CACHE_NAME = 'ai-portfoy-v2';

const STATIC_ASSETS = [
    './',
    './index.html',
    './styles.css',
    './manifest.json',
    './logo.png',
    './js/core/config.js',
    './js/core/i18n.js',
    './js/core/state.js',
    './js/core/MathEngine.js',
    './js/utils.js',
    './js/db.js',
    './js/network/HttpClient.js',
    './js/network/api.js',
    './js/network/supabaseClient.js',
    './js/charts.js',
    './js/app.js',
    './js/worker.js'
];

self.addEventListener('install', (event) => {
    // skipWaiting() devralmayı hızlandırır (Beklemeden yeni versiyona geçiş)
    self.skipWaiting();
    
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
    );
});

self.addEventListener('activate', (event) => {
    // Eski önbellekleri acımasızca temizle (Cache Busting)
    event.waitUntil(
        caches.keys().then((cacheNames) => {
            return Promise.all(
                cacheNames.map((cacheName) => {
                    if (cacheName !== CACHE_NAME) {
                        console.log(`[Service Worker] Eski önbellek siliniyor: ${cacheName}`);
                        return caches.delete(cacheName);
                    }
                })
            );
        }).then(() => {
            // Kontrolü hemen ele al
            return self.clients.claim();
        })
    );
});

self.addEventListener('fetch', (event) => {
    const url = new URL(event.request.url);
    
    // API POST calls (Analysis, Chat vb.) → Network First with Timeout Catch
    if (url.pathname.startsWith('/api/')) {
        event.respondWith(
            fetch(event.request).catch(() => new Response(JSON.stringify({ error: 'Bağlantı koptu veya sunucu uyanamadı.' }), {
                status: 503,
                headers: { 'Content-Type': 'application/json' }
            }))
        );
        return;
    }

    // Static assets & Diğer her şey → Stale-While-Revalidate Stratejisi
    event.respondWith(
        caches.match(event.request).then((cachedResponse) => {
            const fetchPromise = fetch(event.request)
                .then((networkResponse) => {
                    // Sadece başarılı ve geçerli yanıtları önbelleğe al
                    if (networkResponse && networkResponse.ok) {
                        const responseToCache = networkResponse.clone();
                        caches.open(CACHE_NAME).then((cache) => {
                            cache.put(event.request, responseToCache);
                        });
                    }
                    return networkResponse;
                })
                .catch((err) => {
                    console.warn(`[SW] Fetch Hatası: ${event.request.url}`, err);
                    // Eğer cache varsa onu dön, yoksa bir hata Response'u dön ki 'Failed to convert value' hatası almayalım
                    if (cachedResponse) return cachedResponse;
                    throw err; // Veya new Response(...)
                });
            
            return cachedResponse || fetchPromise;
        })
    );
});
