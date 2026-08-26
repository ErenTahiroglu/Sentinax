import { createClient } from 'https://cdn.jsdelivr.net/npm/@supabase/supabase-js/+esm';

// Supabase URL ve Anon Key (Globals from config.js)
const SUPABASE_URL = window.SUPABASE_URL;
const SUPABASE_ANON_KEY = window.SUPABASE_ANON_KEY;

// İstemci Başlat (Client Init) — Singleton Pattern
let supabase = window.supabaseInstance || null;

try {
    if (!supabase && SUPABASE_URL && SUPABASE_URL !== "YOUR_SUPABASE_URL") {
        supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);
        window.supabaseInstance = supabase;
    } else if (!SUPABASE_URL || SUPABASE_URL === "YOUR_SUPABASE_URL") {
        console.warn("Supabase NOT Initialized: URL and Key placeholders still active.");
    }
} catch (e) {
    console.error("Supabase Init Error:", e);
}

// ── Hata çevirisi (Supabase İngilizce hata → Türkçe kullanıcı mesajı) ────────
const _AUTH_ERRORS = {
    "Invalid login credentials": "E-posta veya şifre hatalı. Lütfen tekrar deneyin.",
    "Email not confirmed": "E-posta adresiniz henüz doğrulanmamış. Gelen kutunuzu kontrol edin.",
    "User already registered": "Bu e-posta adresi zaten kayıtlı. Giriş yapmayı deneyin.",
    "Password should be at least 6 characters": "Şifre en az 6 karakter olmalıdır.",
    "Unable to validate email address": "Geçersiz e-posta adresi formatı.",
    "Email rate limit exceeded": "Çok fazla deneme yapıldı. Birkaç dakika sonra tekrar deneyin.",
    "over_email_send_rate_limit": "E-posta gönderme limiti aşıldı. Lütfen bekleyin.",
};

function _friendlyAuthError(err) {
    if (!err) return "Bilinmeyen bir hata oluştu.";
    const msg = err.message || String(err);
    for (const [key, tr] of Object.entries(_AUTH_ERRORS)) {
        if (msg.includes(key)) return tr;
    }
    return msg;
}

// ── Auth İşlemleri ───────────────────────────────────────────────────

export async function signInWithGoogle() {
    if (!supabase) return alert("Supabase Ayarlarını Yapın!");
    const { error } = await supabase.auth.signInWithOAuth({
        provider: 'google',
        options: { redirectTo: window.location.origin }
    });
    if (error) {
        if (typeof showToast === "function") showToast(_friendlyAuthError(error), "error");
        console.error("Google Login Error:", error);
    }
}

/** Fix 2: Email/Password Giriş */
export async function signInWithEmail(email, password) {
    if (!supabase) return { user: null, error: { message: "Supabase bağlı değil." } };
    const { data, error } = await supabase.auth.signInWithPassword({ email, password });
    return { user: data?.user || null, error };
}

/** Fix 2: Email/Password Kayıt Ol */
export async function signUpWithEmail(email, password) {
    if (!supabase) return { user: null, error: { message: "Supabase bağlı değil." } };
    const { data, error } = await supabase.auth.signUp({ email, password });
    return { user: data?.user || null, error };
}

/** Fix 5: SignOut — tüm app-level localStorage anahtarlarını temizler */
export async function signOut() {
    if (!supabase) return;
    const { error } = await supabase.auth.signOut();
    if (error) {
        console.error("Logout Error:", error);
        if (typeof showToast === "function") showToast(_friendlyAuthError(error), "error");
        return;
    }
    // Tüm uygulama state'ini temizle
    const keysToRemove = ["viewMode", "isHalalOnly", "_gk", "_ak", "portfolioWatchlists", "admin_bypass"];
    keysToRemove.forEach(k => localStorage.removeItem(k));
    Object.keys(localStorage)
        .filter(k => k.startsWith("offline_portfolio_"))
        .forEach(k => localStorage.removeItem(k));
    window.location.reload();
}

export async function getUser() {
    if (!supabase) return null;
    try {
        const { data } = await supabase.auth.getUser();
        if (data?.user) return data.user;
    } catch (e) {
        console.warn("Auth check failed:", e);
    }
    
    // Check bypass ONLY if no real user persists
    if (typeof localStorage !== "undefined" && localStorage.getItem("admin_bypass") === "true") {
        return { id: "dev_admin", email: "admin@local", user_metadata: { full_name: "Yönetici (Bypass)" } };
    }
    return null;
}

export async function getValidSession() {
    try {
        if (supabase) {
            const { data } = await supabase.auth.getSession();
            if (data?.session) return data.session;
        }
    } catch (e) { /* ignore */ }

    if (typeof localStorage !== "undefined" && localStorage.getItem("admin_bypass") === "true") {
        return { 
            access_token: "mock_bypass_token",
            user: { id: "dev_admin", email: "admin@local" } 
        };
    }
    return null;
}

// ── UI Reactivity — oturum durumuna göre butonları reaktif günceller ─────────

/** Fix 4: Oturum durumuna göre landing/nav butonlarını günceller */
export function updateAuthUI(user) {
    const navBtn = document.getElementById("landing-nav-login-btn");
    const actionBtn = document.getElementById("landing-action-btn");
    const guestLogoutBtn = document.getElementById("guest-logout-btn");

    if (user) {
        const label = user.email ? user.email.split("@")[0] : "Hesabım";
        if (navBtn) {
            navBtn.innerHTML = `<i class="fas fa-user-circle"></i> ${label}`;
            navBtn.onclick = () => {
                if (typeof showToast === "function") showToast("Çıkış yapılıyor...", "info");
                signOut();
            };
        }
        if (actionBtn) {
            actionBtn.innerHTML = `Uygulamaya Git <i class="fas fa-arrow-right" style="margin-left:8px;"></i>`;
            actionBtn.onclick = () => {
                const landing = document.getElementById("landing-page");
                const sidebar = document.getElementById("sidebar");
                const main = document.querySelector(".main-content");
                if (landing) landing.style.display = "none";
                if (sidebar) sidebar.style.display = "";
                if (main) main.style.display = "";
            };
        }
        if (guestLogoutBtn) guestLogoutBtn.style.display = "none";
        const sidebarUserEl = document.getElementById("sidebar-user-email");
        if (sidebarUserEl) sidebarUserEl.textContent = user.email || "";
    } else {
        if (navBtn) {
            navBtn.innerHTML = `<i class="fas fa-sign-in-alt"></i> Giriş Yap`;
            navBtn.onclick = () => document.getElementById("login-modal")?.classList.remove("hidden");
        }
        if (actionBtn) {
            actionBtn.innerHTML = `Giriş Yap / Kayıt Ol <i class="fas fa-sign-in-alt" style="margin-left:8px;"></i>`;
            actionBtn.onclick = () => document.getElementById("login-modal")?.classList.remove("hidden");
        }
    }
}

// ── Auth Modal Controller ────────────────────────────────────────────────────

let _authMode = "login"; // "login" | "register"

function _showAuthError(msg) {
    const box = document.getElementById("auth-error-box");
    const span = document.getElementById("auth-error-msg");
    const successBox = document.getElementById("auth-success-box");
    if (box && span) { span.textContent = msg; box.style.display = "flex"; }
    if (successBox) successBox.style.display = "none";
}

function _showAuthSuccess(msg) {
    const box = document.getElementById("auth-success-box");
    const span = document.getElementById("auth-success-msg");
    const errBox = document.getElementById("auth-error-box");
    if (box && span) { span.textContent = msg; box.style.display = "flex"; }
    if (errBox) errBox.style.display = "none";
}

function _clearAuthFeedback() {
    const eb = document.getElementById("auth-error-box");
    const sb = document.getElementById("auth-success-box");
    if (eb) eb.style.display = "none";
    if (sb) sb.style.display = "none";
}

window.switchAuthTab = function(mode) {
    _authMode = mode;
    _clearAuthFeedback();
    const loginTab = document.getElementById("auth-tab-login");
    const registerTab = document.getElementById("auth-tab-register");
    const submitBtn = document.getElementById("auth-submit-btn");
    const pwInput = document.getElementById("auth-password");

    const activeBase = "flex:1;padding:0.55rem;border:none;border-radius:8px;font-size:0.85rem;font-weight:600;cursor:pointer;transition:all 0.2s;";
    const activeOn = activeBase + "background:var(--primary);color:#fff;box-shadow:0 2px 8px rgba(99,102,241,0.3);";
    const activeOff = activeBase + "background:transparent;color:var(--text-muted);box-shadow:none;";

    if (mode === "login") {
        if (loginTab) loginTab.style.cssText = activeOn;
        if (registerTab) registerTab.style.cssText = activeOff;
        if (submitBtn) submitBtn.innerHTML = '<i class="fas fa-sign-in-alt"></i> <span id="auth-submit-label">Giriş Yap</span>';
        if (pwInput) pwInput.autocomplete = "current-password";
    } else {
        if (registerTab) registerTab.style.cssText = activeOn;
        if (loginTab) loginTab.style.cssText = activeOff;
        if (submitBtn) submitBtn.innerHTML = '<i class="fas fa-user-plus"></i> <span id="auth-submit-label">Hesap Oluştur</span>';
        if (pwInput) pwInput.autocomplete = "new-password";
    }
};

window.togglePasswordVisibility = function() {
    const pw = document.getElementById("auth-password");
    const eye = document.getElementById("auth-pw-eye");
    if (!pw) return;
    if (pw.type === "password") {
        pw.type = "text";
        if (eye) { eye.classList.remove("fa-eye-slash"); eye.classList.add("fa-eye"); }
    } else {
        pw.type = "password";
        if (eye) { eye.classList.remove("fa-eye"); eye.classList.add("fa-eye-slash"); }
    }
};

/** Fix 1+3: Auth modalını tüm event'lerle birlikte kurar */
export function setupAuthModal() {
    const modal = document.getElementById("login-modal");
    const closeBtn = document.getElementById("auth-modal-close");
    const form = document.getElementById("auth-form");
    const googleBtn = document.getElementById("auth-google-btn");

    if (!modal) { console.warn("setupAuthModal: #login-modal bulunamadı."); return; }

    // Kapatma
    if (closeBtn) closeBtn.addEventListener("click", () => modal.classList.add("hidden"));
    modal.addEventListener("click", (e) => { if (e.target === modal) modal.classList.add("hidden"); });
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && !modal.classList.contains("hidden")) modal.classList.add("hidden");
    });

    // Form submit — Fix 3: kullanıcıya hata göster
    if (form) {
        form.addEventListener("submit", async (e) => {
            e.preventDefault();
            _clearAuthFeedback();
            const email = document.getElementById("auth-email")?.value?.trim();
            const password = document.getElementById("auth-password")?.value;
            const submitBtn = document.getElementById("auth-submit-btn");

            if (!email || !password) {
                _showAuthError("Lütfen e-posta ve şifre alanlarını doldurun.");
                return;
            }

            if (submitBtn) { submitBtn.disabled = true; submitBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> İşleniyor...'; }

            try {
                const result = _authMode === "login"
                    ? await signInWithEmail(email, password)
                    : await signUpWithEmail(email, password);

                if (result.error) {
                    _showAuthError(_friendlyAuthError(result.error));
                } else if (_authMode === "register" && !result.user?.confirmed_at) {
                    _showAuthSuccess("Hesabınız oluşturuldu! E-postanızdaki doğrulama linkine tıklayın.");
                } else {
                    _showAuthSuccess("Giriş başarılı! Yönlendiriliyorsunuz...");
                    updateAuthUI(result.user);
                    setTimeout(async () => {
                        // Onboarding wizard entegrasyon noktası:
                        // Yeni kullanıcı → wizard göster, eski kullanıcı → direkt geç
                        try {
                            const { initOnboardingWizard } = await import('./services/OnboardingWizard.js');
                            await initOnboardingWizard(() => {
                                modal.classList.add("hidden");
                                const landing = document.getElementById("landing-page");
                                const sidebar = document.getElementById("sidebar");
                                const main = document.querySelector(".main-content");
                                if (landing) landing.style.display = "none";
                                if (sidebar) sidebar.style.display = "";
                                if (main) main.style.display = "";
                            });
                        } catch (e) {
                            // Wizard yüklenemezse direkt dashboard'a geç
                            console.error("Wizard init failed, falling back:", e);
                            modal.classList.add("hidden");
                            const landing = document.getElementById("landing-page");
                            const sidebar = document.getElementById("sidebar");
                            const main = document.querySelector(".main-content");
                            if (landing) landing.style.display = "none";
                            if (sidebar) sidebar.style.display = "";
                            if (main) main.style.display = "";
                        }
                    }, 1200);
                }
            } catch (err) {
                _showAuthError("Beklenmeyen bir hata oluştu. Lütfen tekrar deneyin.");
                console.error("Auth submit error:", err);
            } finally {
                if (submitBtn) {
                    submitBtn.disabled = false;
                    submitBtn.innerHTML = _authMode === "login"
                        ? '<i class="fas fa-sign-in-alt"></i> <span id="auth-submit-label">Giriş Yap</span>'
                        : '<i class="fas fa-user-plus"></i> <span id="auth-submit-label">Hesap Oluştur</span>';
                }
            }
        });
    }

    if (googleBtn) googleBtn.addEventListener("click", signInWithGoogle);

    // Fix 4: onAuthStateChange — session persistence + reactive UI
    if (supabase) {
        supabase.auth.onAuthStateChange((event, session) => {
            updateAuthUI(session?.user || null);
            if (event === "SIGNED_IN" && typeof showToast === "function") showToast("Hoş geldin! 👋", "success");
            if (event === "SIGNED_OUT" && typeof showToast === "function") showToast("Oturum kapatıldı.", "info");
        });
    }
}

// ── Database İşlemleri ───────────────────────────────────────────────

export async function savePortfolio(tickersArray) {
    const user = await getUser();
    if (!user) throw new Error("Giriş yapmalısınız!");
    try {
        const session = await getValidSession();
        const jwtToken = session ? session.access_token : "";
        const res = await fetch(`${window.API_BASE}/api/portfolio`, {
            method: "POST",
            headers: { "Content-Type": "application/json", "Authorization": `Bearer ${jwtToken}` },
            body: JSON.stringify({ tickers: tickersArray })
        });
        if (!res.ok) {
            const errData = await res.json().catch(() => ({}));
            throw new Error(errData.detail || "Portföy kaydedilemedi.");
        }
        if (typeof localStorage !== "undefined") localStorage.removeItem(`offline_portfolio_${user.id}`);
        return { status: "success" };
    } catch (err) {
        console.warn("Save failed, saving to offline cache fallback:", err);
        if (typeof localStorage !== "undefined") {
            localStorage.setItem(`offline_portfolio_${user.id}`, JSON.stringify({ tickers: tickersArray, timestamp: Date.now() }));
        }
        throw new Error("Çevrimdışı kaydedildi. Ağ bağlantısı geldiğinde otomatik eşitlenecektir.");
    }
}

export async function loadPortfolio() {
    const user = await getUser();
    if (!user) return [];
    let offlineData = null;
    if (typeof localStorage !== "undefined") {
        const cached = localStorage.getItem(`offline_portfolio_${user.id}`);
        if (cached) { try { offlineData = JSON.parse(cached); } catch (e) { /* ignore */ } }
    }
    try {
        const session = await getValidSession();
        const jwtToken = session ? session.access_token : "";
        const res = await fetch(`${window.API_BASE}/api/portfolio`, {
            method: "GET",
            headers: { "Authorization": `Bearer ${jwtToken}` }
        });
        if (!res.ok) throw new Error("Yükleme hatası");
        const data = await res.json();
        const updated_at = data ? data.updated_at : null;
        if (offlineData && data && updated_at) {
            const dbTime = new Date(updated_at).getTime();
            if (offlineData.timestamp > dbTime) {
                console.log("Offline veri daha yeni, Backend'e geri senkronize ediliyor...");
                savePortfolio(offlineData.tickers).catch(console.error);
                return offlineData.tickers;
            }
        } else if (offlineData && (!data || !data.tickers)) {
            console.log("Yeni offline portföy Backend'e yükleniyor...");
            savePortfolio(offlineData.tickers).catch(console.error);
            return offlineData.tickers;
        }
        if (offlineData) localStorage.removeItem(`offline_portfolio_${user.id}`);
        return data ? (data.tickers || []) : [];
    } catch (err) {
        console.warn("Portföy yüklenemedi (Offline olabilir), local cache kullanılıyor:", err);
        return offlineData ? offlineData.tickers : [];
    }
}

// Vanilla JS Fallback
window.SupabaseAuth = { signInWithGoogle, signInWithEmail, signUpWithEmail, signOut, getUser, getValidSession, savePortfolio, loadPortfolio, updateAuthUI, setupAuthModal };
