import { httpClient } from './HttpClient.js';

export async function pollJobResult(jobId, pollingInterval = 3000) {
    if (!jobId) throw new Error("Job ID eksik");
    
    let attempts = 0;
    const maxAttempts = 40; 
    
    while (attempts < maxAttempts) {
        attempts++;
        try {
            const data = await httpClient.get(`/api/status/${jobId}`);
            if (data.status === "COMPLETED") return data.result;
            if (data.status === "ERROR") throw new Error(data.error || "Arkaplan görevinde sunucu hatası.");
            await new Promise(r => setTimeout(r, pollingInterval));
        } catch (err) {
            if (err.status === 404) throw new Error("Arkaplan görevi zaman aşımına uğradı veya bulunamadı.");
            console.warn("[Polling API Error]", err);
            await new Promise(r => setTimeout(r, pollingInterval));
        }
    }
    throw new Error("Zaman aşımı: Sunucu görevi belirtilen sürede (2dk) tamamlayamadı.");
}

export async function checkServerHealth() {
    try {
        const data = await httpClient.get('/api/health');
        return { online: true, message: data.message };
    } catch (e) {
        console.error("Health check failed:", e);
        return { online: false, message: e.message || "Sunucuya ulaşılamıyor." };
    }
}
