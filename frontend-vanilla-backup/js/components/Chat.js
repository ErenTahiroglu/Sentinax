// ═══════════════════════════════════════
// AI COPILOT CHAT MODULE
// ═══════════════════════════════════════

const API_BASE = window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1" 
    ? "http://localhost:8000" 
    : "https://ai-portfoy-yoneticisi.onrender.com";

let chatHistory = [];
let macroBuffer = "";

export function initCopilot() {
    const fab = document.getElementById("copilot-fab");
    const widget = document.getElementById("copilot-widget");
    const closeBtn = document.getElementById("copilot-close-btn");
    const input = document.getElementById("copilot-input");
    const sendBtn = document.getElementById("copilot-send-btn");
    const body = document.getElementById("copilot-body");
    
    if (!fab || !widget) return;

    // Show FAB if AI is enabled
    document.getElementById("use-ai-toggle").addEventListener("change", (e) => {
        const lastResults = window.lastResults || (typeof AppState !== "undefined" && AppState.results) || [];
        if (e.target.checked && lastResults.length > 0) fab.classList.remove("hidden");
        else { fab.classList.add("hidden"); widget.classList.add("hidden"); }
    });
    
    fab.addEventListener("click", () => {
        widget.classList.toggle("hidden");
        if (!widget.classList.contains("hidden") && input) input.focus();
    });
    
    if (closeBtn) closeBtn.addEventListener("click", () => widget.classList.add("hidden"));
    
    function appendMsg(text, isUser) {
        // 🛡️ SRE DOM Pruning to prevent Memory Leaks in long sessions
        while (body.children.length >= 50) {
            body.removeChild(body.firstChild);
        }

        const div = document.createElement("div");
        div.className = `copilot-msg ${isUser ? 'user-msg' : 'ai-msg'}`;
        div.innerHTML = isUser ? text : (typeof marked !== "undefined" ? marked.parse(text) : text); 
        body.appendChild(div);
        body.scrollTop = body.scrollHeight;
    }
    
    async function sendMessage() {
        const text = input.value.trim();
        if (!text) return;
        
        // 🛡️ SRE Stale State / Slippage Guard on wakeup
        if (window.lastPricesUpdatedAt) {
            const diff = Date.now() - window.lastPricesUpdatedAt;
            if (diff > 5 * 10 * 1000) { // 50 saniye (Sık güncellenen piyasa için daha hassas)
                if (typeof showToast === "function") {
                    showToast("🚀 Canlı fiyatlar güncel değil (Stale). WebSocket yeniden bağlanıyor, lütfen bekleyin.", "warning");
                }
                return;
            }
        }

        const apiKey = document.getElementById("api-key").value;
        if (!apiKey) {
            if (typeof showToast === "function") showToast("AI bağlantısı için API anahtarı gereklidir.", "warning");
            return;
        }
        
        appendMsg(text, true);
        chatHistory.push({ role: "user", content: text });
        if (chatHistory.length > 5) chatHistory = chatHistory.slice(-5);
        input.value = "";
        
        // Show loading indicator
        const loadDiv = document.createElement("div");
        loadDiv.className = "copilot-msg ai-msg";
        loadDiv.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Düşünüyor...';
        body.appendChild(loadDiv);
        body.scrollTop = body.scrollHeight;
        
        try {
            const lastResults = window.lastResults || (typeof AppState !== "undefined" && AppState.results) || [];
            const lastExtras = window.lastExtras || (typeof AppState !== "undefined" && AppState.extras) || {};
            
            const userProfile = typeof window.getUserProfile === "function" ? window.getUserProfile() : null;

            // ── Telemetry: Kullanıcı Tepkisi Takibi ──
            if (window.lastAiMessageWasBrake) {
                // Basit bir RegEx ile kullanıcının ısrar edip etmediğini anla (Is Ignored?)
                const isIgnored = /sat|yap|onayla|boşver|istiyorum|yine de/i.test(text.toLowerCase());
                const eventType = isIgnored ? 'brake_ignored' : 'brake_accepted';
                
                logTelemetryEvent(eventType, { user_reply: text });
                window.lastAiMessageWasBrake = false;
            }

            const contextMsg = {
                results: lastResults.map(r => ({ ticker: r.ticker, metrics: r.valuation, risk: r.financials?.risk, performance: r.financials?.s5 })),
                extras: lastExtras
            };
            
            const payload = {
                messages: chatHistory,
                portfolio_context: contextMsg,
                api_key: apiKey,
                model: document.getElementById("model-select").value,
                lang: typeof getLang === "function" ? getLang() : "tr",
                user_profile: userProfile
            };
            
            let jwtToken = "";
            try {
                const session = await window.SupabaseAuth.getValidSession();
                if (session) jwtToken = session.access_token;
            } catch (e) {
                loadDiv.remove();
                appendMsg("Güvenlik Hatası: " + e.message, false);
                return;
            }
            
            const res = await fetch(`${window.API_BASE}/api/chat`, {
                method: "POST",
                headers: { 
                    "Content-Type": "application/json",
                    "Authorization": `Bearer ${jwtToken}`
                },
                body: JSON.stringify(payload)
            });
            
            let reply = "";
            const initialData = await res.json();
            if (res.status === 202 && initialData.job_id) {
                 // Polling başlat (Vercel kalkanı)
                 const jobData = await window.pollJobResult(initialData.job_id, 3000);
                 
                 loadDiv.remove();
                 
                 reply = jobData.result || jobData.reply || jobData.response;
                 if (reply) {
                     appendMsg(reply, false);
                     chatHistory.push({ role: "assistant", content: reply });
                 } else {
                     throw new Error("Geçerli bir yanıt alınamadı.");
                 }
            } else {
                 loadDiv.remove();
                 if (!res.ok) throw new Error(initialData.detail || "API Hatası");
                 
                 reply = initialData.result || initialData.reply || initialData.response;
                 appendMsg(reply, false);
                 chatHistory.push({ role: "assistant", content: reply });
            }

            // ── Telemetry: AI Frenini Yakala ──
            if (reply && (reply.includes("beklemek ister misiniz") || reply.includes("panikle satmak")) && userProfile?.level === "beginner") {
                window.lastAiMessageWasBrake = true;
            }
        } catch(err) {
            loadDiv.remove();
            appendMsg("Bağlantı hatası: " + err.message, false);
        }
    }

    async function logTelemetryEvent(eventType, metadata = {}) {
        try {
            const session = await window.SupabaseAuth.getValidSession();
            if (!session) return;
            await fetch(`${API_BASE}/api/telemetry/event`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Authorization": `Bearer ${session.access_token}`
                },
                body: JSON.stringify({ event_type: eventType, event_metadata: metadata })
            });
        } catch (e) { console.warn("Telemetry failed:", e); }
    }
    
    if (sendBtn) sendBtn.addEventListener("click", sendMessage);
    if (input) input.addEventListener("keypress", (e) => { if (e.key === "Enter") sendMessage(); });
}

export async function renderMacroAI(chunk, isDone) {
    let container = document.getElementById("macro-advice-container");
    
    if (!container) {
        const grid = document.getElementById("results-grid");
        if (!grid) return; 
        
        const { createMacroCardHolder } = await import('./CardComponent.js');
        container = createMacroCardHolder();
        grid.parentNode.appendChild(container);
        macroBuffer = ""; 
    }

    const contentDiv = document.getElementById("macro-content");

    if (chunk) {
        macroBuffer += chunk;
        if (typeof marked !== "undefined") {
            try {
                contentDiv.innerHTML = marked.parse(macroBuffer);
            } catch (e) {
                contentDiv.innerText = macroBuffer; 
            }
        } else {
            contentDiv.innerText = macroBuffer;
        }
        container.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }

    if (isDone) {
        if (typeof marked !== "undefined") {
            try { contentDiv.innerHTML = marked.parse(macroBuffer); } catch (e) { /* ignore */ }
        }
        if (typeof showToast === "function") showToast("Makro AI Analizi Hazır!", "success");
    }
}
