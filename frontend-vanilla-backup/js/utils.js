// const IS_LOCAL = window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1";
// let lastResults = null;
// let lastExtras = null;
// let chartInstances = {};

// ═══════════════════════════════════════
// TOAST NOTIFICATIONS
// ═══════════════════════════════════════
export function showToast(message, type = "info") {
    const container = document.getElementById("toast-container");
    if (!container) return; // For headless tests
    const icons = { success: "fa-check-circle", error: "fa-exclamation-circle", warning: "fa-exclamation-triangle", info: "fa-info-circle" };
    const toast = document.createElement("div");
    toast.className = `toast toast-${type}`;
    toast.innerHTML = `<i class="fas ${icons[type] || icons.info}"></i><span>${message}</span>`;
    container.appendChild(toast);
    setTimeout(() => toast.remove(), 4000);
}
window.showToast = showToast;

// ═══════════════════════════════════════
// THEME TOGGLE
// ═══════════════════════════════════════
export function initTheme() {
    const saved = localStorage.getItem("theme");
    const systemDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    const themeToApply = saved || (systemDark ? "dark" : "light");
    
    document.documentElement.setAttribute("data-theme", themeToApply);
    updateThemeIcon(themeToApply);
}
window.initTheme = initTheme;

export function toggleTheme() {
    const current = document.documentElement.getAttribute("data-theme");
    const next = current === "light" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("theme", next);
    updateThemeIcon(next);
}
window.toggleTheme = toggleTheme;

function updateThemeIcon(theme) {
    const icon = document.getElementById("theme-icon");
    if (icon) icon.className = theme === "light" ? "fas fa-sun" : "fas fa-moon";
}

// ═══════════════════════════════════════
// WATCHLIST (localStorage)
// ═══════════════════════════════════════
function getWatchlists() {
    try { return JSON.parse(localStorage.getItem("portfolioWatchlists") || "[]"); }
    catch { return []; }
}

function saveWatchlists(lists) {
    localStorage.setItem("portfolioWatchlists", JSON.stringify(lists));
}

function renderWatchlists() {
    const container = document.getElementById("watchlist-container");
    if (!container) return;
    const lists = getWatchlists();
    container.innerHTML = "";
    if (lists.length === 0) {
        container.innerHTML = `<p style="font-size:0.75rem; color:var(--text-muted); text-align:center; padding:0.5rem;">${t("sidebar.noWatchlist")}</p>`;
        return;
    }
    lists.forEach((item, idx) => {
        const el = document.createElement("div");
        el.className = "watchlist-item";
        el.innerHTML = `<div><div class="watchlist-name">${item.name}</div><div class="watchlist-count">${item.tickers.length} hisse</div></div><button class="watchlist-delete" data-idx="${idx}" title="Sil"><i class="fas fa-trash-alt"></i></button>`;
        el.addEventListener("click", (e) => {
            if (e.target.closest(".watchlist-delete")) return;
            document.getElementById("ticker-input").value = item.tickers.join(", ");
            showToast(`"${item.name}" ${t("toast.loaded")}`, "success");
        });
        el.querySelector(".watchlist-delete").addEventListener("click", (e) => {
            e.stopPropagation();
            const updated = getWatchlists().filter((_, i) => i !== idx);
            saveWatchlists(updated);
            renderWatchlists();
            showToast(`"${item.name}" ${t("toast.deleted")}`, "info");
        });
        container.appendChild(el);
    });
}

export function saveCurrentPortfolio() {
    const input = document.getElementById("ticker-input").value.trim();
    if (!input) { showToast(t("toast.enterTickers"), "warning"); return; }
    const tickers = input.split(/[\s,;]+/).filter(t => t.length > 0).map(t => t.toUpperCase());
    const name = prompt(t("toast.portfolioName"));
    if (!name) return;
    const lists = getWatchlists();
    lists.push({ name, tickers });
    saveWatchlists(lists);
    renderWatchlists();
    showToast(`"${name}" ${t("toast.saved")} (${tickers.length} hisse)`, "success");
}
window.saveCurrentPortfolio = saveCurrentPortfolio;

// ═══════════════════════════════════════
// AUTOCOMPLETE
// ═══════════════════════════════════════
let autocompleteTimeout = null;
const autocompleteCache = {};

export function setupAutocomplete() {
    const textarea = document.getElementById("ticker-input");
    const dropdown = document.getElementById("autocomplete-dropdown");
    if (!textarea || !dropdown) return;

    // Dinamik CSS Ekle (Eğer yoksa)
    if (!document.getElementById("autocomplete-style")) {
        const style = document.createElement("style");
        style.id = "autocomplete-style";
        style.innerHTML = `
            .autocomplete-item.selected { background: rgba(14, 165, 233, 0.15) !important; border-left: 2px solid var(--primary); }
            .autocomplete-item.loading { text-align: center; padding: 0.75rem; color: var(--text-muted); font-size: 0.8rem; }
            .ticker-exch { font-size: 0.65rem; padding: 0.1rem 0.3rem; border-radius: 3px; background: rgba(255,255,255,0.05); color: var(--text-muted); }
        `;
        document.head.appendChild(style);
    }

    let selectedIndex = -1;

    // ── BIST30 ve Güvenli Varlıklar (Yeni Başlayanlar İçin Whitelist) ──
    const BIST30 = [
        "AKBNK", "ALARK", "ARCLK", "ASELS", "ASTOR", "BIMAS", "DOAS", "EKGYO", 
        "ENKAI", "EREGL", "FROTO", "GARAN", "GUBRF", "HEKTS", "ISCTR", "KCHOL", 
        "KONTR", "KOZAL", "ODAS", "PGSUS", "PETKM", "SAHOL", "SASA", "SISE", 
        "TCELL", "THYAO", "TOASO", "TUPRS", "VESTL", "YKBNK"
    ];

    function isSafeAsset(s) {
        const profile = typeof window.getUserProfile === "function" ? window.getUserProfile() : null;
        if (!profile || profile.level !== "beginner") return true; 

        const ticker = (s.symbol || "").toUpperCase();
        const exch = (s.exchDisp || "").toUpperCase();

        // 1. Kripto: Sadece BTC ve ETH
        if (exch.includes("CRYPTO")) {
            return ["BTC", "ETH"].includes(ticker);
        }

        // 2. BIST: Sadece BIST30
        if (exch.includes("BIST")) {
            return BIST30.includes(ticker);
        }

        // 3. ABD: Sadece Majörler (Big Tech & S&P Top 10)
        if (exch.includes("US") || exch.includes("NASDAQ") || exch.includes("NYSE")) {
            const US_MAJORS = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK-B", "V", "MA"];
            return US_MAJORS.includes(ticker);
        }

        // 4. TEFAS: Genellikle güvenli kabul edilir (ancak sığ olanlar hariç, basitlik için hepsine izin verelim veya kısıtlayalım)
        if (exch.includes("TEFAS")) return true;

        return false;
    }

    function renderItems(items) {
        const profile = typeof window.getUserProfile === "function" ? window.getUserProfile() : null;
        const isBeginner = profile && profile.level === "beginner";
        
        // Güvenli filtreleme (Beginner ise)
        const visibleItems = isBeginner ? items.filter(isSafeAsset) : items;
        const hiddenCount = items.length - visibleItems.length;

        if (visibleItems.length === 0) {
            if (isBeginner && items.length > 0) {
                // Filtreleme sonucu hepsi gizlendiyse uyarı göster
                dropdown.innerHTML = `
                    <div style="padding:1rem; text-align:center; color:var(--text-muted); font-size:0.8rem; line-height:1.4;">
                        <i class="fas fa-user-shield" style="font-size:1.2rem; color:var(--warning); margin-bottom:0.5rem; display:block;"></i>
                        Bu varlık yüksek volatilite içerdiğinden başlangıç profiliniz için gizlenmiştir. 
                        <strong>BIST30</strong> ve majör varlıklarda işlem yapmanız önerilir.
                    </div>`;
                dropdown.classList.remove("hidden");
            } else {
                dropdown.classList.add("hidden");
            }
            return;
        }

        let html = visibleItems.map((s, i) => `
            <div class="autocomplete-item ${i === selectedIndex ? 'selected' : ''}" data-ticker="${s.symbol}">
                <div style="flex:1; display:flex; align-items:center; gap:8px;" class="autocomplete-clickable">
                    <span class="ticker-symbol">${s.symbol}</span>
                    <span class="ticker-name">${s.name}</span>
                    <span class="ticker-exch">${s.exchDisp || ""}</span>
                </div>
                <i class="fas fa-plus ticker-info-btn" title="Listeye Ekle" data-ticker="${s.symbol}"></i>
            </div>`).join("");

        // Eğer bazı varlıklar gizlendiyse ufak bir bilgilendirme ekle
        if (hiddenCount > 0) {
            html += `<div style="font-size:0.65rem; padding:0.5rem; color:var(--text-muted); border-top:1px solid var(--glass-border); text-align:center; opacity:0.8;">
                <i class="fas fa-eye-slash"></i> ${hiddenCount} riskli varlık profiliniz gereği gizlendi.
            </div>`;
        }

        dropdown.innerHTML = html;
        dropdown.classList.remove("hidden");
        attachItemEvents();
    }

    function attachItemEvents() {
        dropdown.querySelectorAll(".autocomplete-clickable").forEach(el => {
            el.addEventListener("click", (e) => {
                const ticker = e.currentTarget.parentElement.dataset.ticker;
                selectTicker(ticker);
            });
        });
        dropdown.querySelectorAll(".ticker-info-btn").forEach(btn => {
            btn.addEventListener("click", (e) => {
                e.stopPropagation();
                selectTicker(e.currentTarget.dataset.ticker);
            });
        });
    }

    function selectTicker(ticker) {
        const currentWords = textarea.value.split(/([\s,;]+)/);
        let found = false;
        for (let i = currentWords.length - 1; i >= 0; i--) {
            if (currentWords[i].trim() && !found) {
                currentWords[i] = ticker;
                found = true;
                break;
            }
        }
        textarea.value = currentWords.join("") + ", ";
        dropdown.classList.add("hidden");
        textarea.focus();
        selectedIndex = -1;
    }

    textarea.addEventListener("input", () => {
        clearTimeout(autocompleteTimeout);
        const content = textarea.value;
        const words = content.split(/[\s,;]+/);
        const lastWord = words[words.length - 1].trim();

        if (!lastWord || lastWord.length < 1) {
            dropdown.classList.add("hidden");
            return;
        }

        selectedIndex = -1; // Reset selection

        // 1. Önbellek (Cache) Kontrolü
        if (autocompleteCache[lastWord]) {
            renderItems(autocompleteCache[lastWord]);
            return;
        }

        // 2. Skeleton Loading göster
        dropdown.innerHTML = `<div class="autocomplete-item loading"><i class="fas fa-spinner fa-spin"></i> Aranıyor...</div>`;
        dropdown.classList.remove("hidden");

        autocompleteTimeout = setTimeout(async () => {
            try {
                const res = await fetch(`${window.API_BASE}/api/search?q=${encodeURIComponent(lastWord)}`);
                if (!res.ok) throw new Error("API error");
                const data = await res.json();
                
                // Cache kaydet
                autocompleteCache[lastWord] = data;
                
                renderItems(data);
            } catch (err) {
                console.warn("Autocomplete error:", err);
                dropdown.classList.add("hidden");
            }
        }, 300); // 300ms Debounce
    });

    // Klavye Gezinmesi
    textarea.addEventListener("keydown", (e) => {
        const items = dropdown.querySelectorAll(".autocomplete-item");
        if (items.length === 0 || dropdown.classList.contains("hidden")) return;

        if (e.key === "ArrowDown") {
            e.preventDefault();
            selectedIndex = (selectedIndex + 1) % items.length;
            updateSelection(items);
        } else if (e.key === "ArrowUp") {
            e.preventDefault();
            selectedIndex = (selectedIndex - 1 + items.length) % items.length;
            updateSelection(items);
        } else if (e.key === "Enter") {
            if (selectedIndex >= 0 && selectedIndex < items.length) {
                e.preventDefault();
                selectTicker(items[selectedIndex].dataset.ticker);
            }
        } else if (e.key === "Escape") {
            dropdown.classList.add("hidden");
        }
    });

    function updateSelection(items) {
        items.forEach((item, i) => {
            if (i === selectedIndex) {
                item.classList.add("selected");
                item.scrollIntoView({ block: "nearest" });
            } else {
                item.classList.remove("selected");
            }
        });
    }

    // Click Outside To Close
    document.addEventListener("click", (e) => {
        if (!textarea.contains(e.target) && !dropdown.contains(e.target)) {
            dropdown.classList.add("hidden");
        }
    });

    // Input kutusuna tekrar tıklandığında eski sonuçları aç
    textarea.addEventListener("click", () => {
        const words = textarea.value.split(/[\s,;]+/);
        const lastWord = words[words.length - 1].trim();
        if (lastWord && autocompleteCache[lastWord]) {
            renderItems(autocompleteCache[lastWord]);
        }
    });
}
window.setupAutocomplete = setupAutocomplete;

// ═══════════════════════════════════════
// COLLAPSIBLE SECTIONS
// ═══════════════════════════════════════
export function toggleCollapsible(header) {
    header.classList.toggle("open");
    header.nextElementSibling.classList.toggle("open");
}
window.toggleCollapsible = toggleCollapsible;

// ═══════════════════════════════════════
// FORMAT HELPERS
// ═══════════════════════════════════════
export function formatMarketCap(val) {
    if (!val) return "-";
    if (val >= 1e12) return (val / 1e12).toFixed(2) + "T";
    if (val >= 1e9) return (val / 1e9).toFixed(2) + "B";
    if (val >= 1e6) return (val / 1e6).toFixed(1) + "M";
    return val.toLocaleString();
}

export function fmtNum(val, suffix = "") {
    if (val === undefined || val === null || val === "-") return "-";
    const num = typeof val === "number" ? val : parseFloat(val);
    if (isNaN(num)) return "-";
    
    // 🛡️ Locale-Aware Formatting (TR/US auto formats)
    const locale = (typeof getLang === "function" && getLang() === "en") ? "en-US" : "tr-TR";
    try {
         return new Intl.NumberFormat(locale, {
             minimumFractionDigits: 2,
             maximumFractionDigits: 2
         }).format(num) + suffix;
    } catch {
         return num.toFixed(2) + suffix;
    }
}
window.fmtNum = fmtNum;

export function colorClass(val) {
    if (val === undefined || val === null) return "";
    return val >= 0 ? "positive" : "negative";
}
window.colorClass = colorClass;

// ═══════════════════════════════════════
// API KEY ENCRYPTION (AES-GCM)
// ═══════════════════════════════════════
async function getEncKey() {
    const raw = localStorage.getItem("_ek");
    if (raw) return await crypto.subtle.importKey("jwk", JSON.parse(raw), { name: "AES-GCM" }, true, ["encrypt", "decrypt"]);
    const key = await crypto.subtle.generateKey({ name: "AES-GCM", length: 256 }, true, ["encrypt", "decrypt"]);
    localStorage.setItem("_ek", JSON.stringify(await crypto.subtle.exportKey("jwk", key)));
    return key;
}

export async function encryptApiKey(plaintext) {
    if (!plaintext) return "";
    const key = await getEncKey();
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const ct = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, new TextEncoder().encode(plaintext));
    const combined = new Uint8Array(iv.length + ct.byteLength);
    combined.set(iv); combined.set(new Uint8Array(ct), iv.length);
    return btoa(String.fromCharCode(...combined));
}
window.encryptApiKey = encryptApiKey;

export async function decryptApiKey(b64) {
    if (!b64) return "";
    try {
        const key = await getEncKey();
        const data = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
        const iv = data.slice(0, 12);
        const ct = data.slice(12);
        const pt = await crypto.subtle.decrypt({ name: "AES-GCM", iv }, key, ct);
        return new TextDecoder().decode(pt);
    } catch { return ""; }
}
window.decryptApiKey = decryptApiKey;

export async function saveApiKeys() {
    const gemini = document.getElementById("api-key").value;
    const av = document.getElementById("av-api-key").value;
    if (gemini) {
        localStorage.setItem("_gk", await encryptApiKey(gemini));
        const icon = document.getElementById("api-key-saved-icon");
        if (icon) { icon.classList.remove("hidden"); setTimeout(() => icon.classList.add("hidden"), 3000); }
    }
    if (av) {
        localStorage.setItem("_ak", await encryptApiKey(av));
        const icon = document.getElementById("av-key-saved-icon");
        if (icon) { icon.classList.remove("hidden"); setTimeout(() => icon.classList.add("hidden"), 3000); }
    }
}
window.saveApiKeys = saveApiKeys;

export async function loadApiKeys() {
    const gk = await decryptApiKey(localStorage.getItem("_gk") || "");
    const ak = await decryptApiKey(localStorage.getItem("_ak") || "");
    if (gk) {
        const el = document.getElementById("api-key");
        if (el) el.value = gk;
        const icon = document.getElementById("api-key-saved-icon");
        if (icon) icon.classList.remove("hidden");
    }
    if (ak) {
        const el = document.getElementById("av-api-key");
        if (el) el.value = ak;
        const icon = document.getElementById("av-key-saved-icon");
        if (icon) icon.classList.remove("hidden");
    }
}
window.loadApiKeys = loadApiKeys;

// ═══════════════════════════════════════
// TICKER QUICK VIEW MODAL
// ═══════════════════════════════════════
export async function showTickerQuickModal(ticker) {
    let overlay = document.getElementById("ticker-modal-overlay");
    if (!overlay) {
        overlay = document.createElement("div");
        overlay.id = "ticker-modal-overlay";
        overlay.className = "modal-overlay";
        document.body.appendChild(overlay);
        overlay.addEventListener("click", (e) => {
            if (e.target === overlay) overlay.remove();
        });
    }

    // Yükleme durumu (Skeleton UI)
    overlay.innerHTML = `
        <div class="modal-content glass-panel" style="animation: slideUpFade 0.3s ease;">
            <button class="modal-close" id="ticker-modal-close-btn"><i class="fas fa-times"></i></button>
            <h3 style="margin-bottom:1rem;font-size:1.2rem;color:var(--text-main);"><i class="fas fa-search"></i> ${ticker} Hızlı Görünüm</h3>
            <div class="skeleton-title" style="width: 40%"></div>
            <div class="skeleton-box" style="margin-bottom: 1rem;"></div>
            <div class="skeleton-text"></div>
            <div class="skeleton-text" style="width: 80%"></div>
        </div>
    `;
    document.getElementById('ticker-modal-close-btn').onclick = () => overlay.remove();

    try {
        const payload = {
            tickers: [ticker],
            use_ai: false,
            api_key: "",
            av_api_key: "",
            model: "gemini-2.5-flash",
            check_islamic: document.getElementById("check-islamic-toggle").checked,
            check_financials: true,
            lang: getLang()
        };
        const res = await fetch(`${window.API_BASE}/api/analyze`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        const r = data.results && data.results[0];

        if (!r || r.error) {
            overlay.innerHTML = `
                <div class="modal-content glass-panel">
                    <button class="modal-close" id="ticker-modal-err-close"><i class="fas fa-times"></i></button>
                    <h3 style="margin-bottom:1rem;"><i class="fas fa-exclamation-triangle" style="color:var(--danger)"></i> ${ticker}</h3>
                    <p style="color:var(--danger);">${r?.error || "Veri alınamadı."}</p>
                </div>
            `;
            document.getElementById('ticker-modal-err-close').onclick = () => overlay.remove();
            return;
        }

        const fin = r.financials || {};
        const val = r.valuation || {};
        const price = fin.son_fiyat?.fiyat ? fin.son_fiyat.fiyat.toFixed(2) : "-";
        const ccy = fin.son_fiyat?.para_birimi || "";
        const mcap = val.market_cap ? formatMarketCap(val.market_cap) : "-";

        let statusText = r.status || "-";
        if (getLang() === "en") {
            if (statusText === "Uygun") statusText = "Compliant";
            else if (statusText === "Uygun Değil") statusText = "Non-Compliant";
            else if (statusText === "Katılım Fonu Değil") statusText = "Non-Participation";
        }
        const statusHTML = statusText !== "-" ? `<span style="padding:0.2rem 0.5rem; border-radius:4px; font-size:0.75rem; background:rgba(34,197,94,0.1); color:var(--success); border:1px solid rgba(34,197,94,0.2);">${statusText}</span>` : "";

        overlay.innerHTML = `
            <div class="modal-content glass-panel" style="animation: slideUpFade 0.3s ease;">
                <button class="modal-close" id="ticker-modal-data-close"><i class="fas fa-times"></i></button>
                <div style="display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:1.5rem;">
                    <div>
                        <h2 style="font-size:1.5rem;font-weight:800;letter-spacing:-0.5px;color:var(--text-main); margin-bottom:0.2rem;">${r.ticker}</h2>
                        <div style="font-size:0.85rem;color:var(--text-muted);">${r.full_name || fin.ad || ""}</div>
                    </div>
                    <div style="text-align:right;">
                        <div style="font-size:1.5rem;font-weight:700;color:var(--primary);">${price} ${ccy}</div>
                        ${statusHTML}
                    </div>
                </div>
                
                <div style="display:grid; grid-template-columns:1fr 1fr; gap:1rem; margin-bottom:1.5rem;">
                    <div style="background:rgba(14,165,233,0.05); padding:1rem; border-radius:8px; border:1px solid var(--glass-border);">
                        <div style="font-size:0.7rem; color:var(--text-muted); text-transform:uppercase;">P/E</div>
                        <div style="font-size:1.1rem; font-weight:600; color:var(--text-main);">${fmtNum(val.pe)}</div>
                    </div>
                    <div style="background:rgba(14,165,233,0.05); padding:1rem; border-radius:8px; border:1px solid var(--glass-border);">
                        <div style="font-size:0.7rem; color:var(--text-muted); text-transform:uppercase;">P/B</div>
                        <div style="font-size:1.1rem; font-weight:600; color:var(--text-main);">${fmtNum(val.pb)}</div>
                    </div>
                    <div style="background:rgba(14,165,233,0.05); padding:1rem; border-radius:8px; border:1px solid var(--glass-border);">
                        <div style="font-size:0.7rem; color:var(--text-muted); text-transform:uppercase;">Piyasa Değeri</div>
                        <div style="font-size:1.1rem; font-weight:600; color:var(--text-main);">${mcap}</div>
                    </div>
                    <div style="background:rgba(14,165,233,0.05); padding:1rem; border-radius:8px; border:1px solid var(--glass-border);">
                        <div style="font-size:0.7rem; color:var(--text-muted); text-transform:uppercase;">Beta</div>
                        <div style="font-size:1.1rem; font-weight:600; color:var(--text-main);">${fmtNum(val.beta)}</div>
                    </div>
                </div>
                
                <button class="btn-primary" id="ticker-modal-add-btn">
                    <i class="fas fa-plus"></i> Listeye Ekle
                </button>
            </div>
        `;
        document.getElementById('ticker-modal-data-close').onclick = () => overlay.remove();
        document.getElementById('ticker-modal-add-btn').onclick = () => {
            const tarea = document.getElementById('ticker-input');
            let words = tarea.value.split(/[\s,;]+/);
            words = words.filter(w=>w.trim());
            if (words[words.length-1] !== r.ticker) words.push(r.ticker);
            tarea.value = words.join(', ') + ', ';
            overlay.remove();
            tarea.focus();
        };
    } catch (err) {
        overlay.innerHTML = `
            <div class="modal-content glass-panel">
                <button class="modal-close" id="ticker-modal-err-final-close"><i class="fas fa-times"></i></button>
                <h3 style="margin-bottom:1rem;color:var(--danger)"><i class="fas fa-times-circle"></i> Hatası</h3>
                <p>Beklenmeyen bir hata oluştu: ${err.message}</p>
            </div>
        `;
        document.getElementById('ticker-modal-err-final-close').onclick = () => overlay.remove();
    }
}
window.showTickerQuickModal = showTickerQuickModal;
