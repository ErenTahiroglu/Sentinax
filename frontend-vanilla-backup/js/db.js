// IndexedDB wrapper for caching portfolio analysis
(function() {
    const DB_NAME = "PortfolioCacheDB";
    const STORE_NAME = "analysisCache";
    const DB_VERSION = 2; // Incremented version to ensure upgrade

    function initDB() {
        return new Promise((resolve, reject) => {
            if (!window.indexedDB) {
                console.warn("IndexedDB not supported");
                resolve(null);
                return;
            }
            const request = indexedDB.open(DB_NAME, DB_VERSION);
            request.onupgradeneeded = (e) => {
                const db = e.target.result;
                if (!db.objectStoreNames.contains(STORE_NAME)) {
                    db.createObjectStore(STORE_NAME, { keyPath: "id" });
                }
            };
            request.onsuccess = (e) => resolve(e.target.result);
            request.onerror = (e) => reject(e.target.error);
        });
    }

    window.getCache = async function(id) {
        try {
            const db = await initDB();
            if (!db) return null;
            return new Promise((resolve) => {
                const tx = db.transaction(STORE_NAME, "readonly");
                const store = tx.objectStore(STORE_NAME);
                const request = store.get(id);
                request.onsuccess = () => {
                    const res = request.result;
                    if (res && res.data) {
                        // 12 hours TTL (43200000 ms)
                        if (Date.now() - res.timestamp < 43200000) {
                            resolve(res.data);
                        } else {
                            // Expired
                            resolve(null);
                        }
                    } else {
                        resolve(null);
                    }
                };
                request.onerror = () => resolve(null);
            });
        } catch (err) {
            console.error("IndexedDB Get Error:", err);
            return null;
        }
    };

    window.setCache = async function(id, data) {
        try {
            const db = await initDB();
            if (!db) return false;
            const tx = db.transaction(STORE_NAME, "readwrite");
            const store = tx.objectStore(STORE_NAME);
            store.put({ id, data, timestamp: Date.now() });
            return new Promise((resolve) => {
                tx.oncomplete = () => resolve(true);
                tx.onerror = () => resolve(false);
            });
        } catch (err) {
            console.error("IndexedDB Set Error:", err);
            return false;
        }
    };
})();
