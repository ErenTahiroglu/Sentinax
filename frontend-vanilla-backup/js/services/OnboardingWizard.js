// ═══════════════════════════════════════════════════════════════════════════
// ONBOARDING WIZARD SERVICE  v1.0
// ═══════════════════════════════════════════════════════════════════════════
// Kullanıcının ilk girişinde 3 adımlı, jargonsuz bir karşılama sihirbazı
// gösterir. Seçimler Supabase'e kaydedilir; localStorage yalnızca cache.
//
// Dışa aktarılan API:
//   initOnboardingWizard(onCompleteCb)  → sihirbazı başlatır veya atlar
//   getUserProfile()                    → localStorage'dan profili döndürür
// ═══════════════════════════════════════════════════════════════════════════

const PROFILE_KEY = "onboarding_profile";

// Adım tanımları — sıfır jargon
const STEPS = [
    {
        key:   "level",
        meta:  "Adım 1 / 3 — Seni tanıyalım",
        title: "Yatırım konusundaki durumunu en iyi hangisi anlatıyor?",
        cards: [
            { value: "beginner", emoji: "🌱", label: "Hiç başlamadım.",    desc: "Birikimim var ama ne yapacağımı bilmiyorum." },
            { value: "read",     emoji: "📖", label: "Biraz okudum.",      desc: "Birkaç kavramı duydum ama hiç işlem yapmadım." },
            { value: "tried",    emoji: "🔄", label: "Denedim, öğreniyorum.", desc: "Denemeler yaptım, daha sistematik olmak istiyorum." },
        ],
    },
    {
        key:   "goal",
        meta:  "Adım 2 / 3 — Hedefin",
        title: "Bu para senin için ne anlama geliyor?",
        cards: [
            { value: "protect", emoji: "🛡️", label: "Paramın erimesini durdurmak istiyorum.", desc: "Enflasyona karşı koruma, o kadar." },
            { value: "target",  emoji: "🏠", label: "Belirli bir hedefim var.",              desc: "Ev, araba, tatil, çocuk eğitimi..." },
            { value: "grow",    emoji: "📈", label: "Paramın büyümesini istiyorum.",         desc: "Uzun vadeli, sabırlı bir artış." },
        ],
    },
    {
        key:   "riskTolerance",
        meta:  "Adım 3 / 3 — Risk hissi",
        title: "Yatırdığın paranın bir süre düşme ihtimali olsa ne tepki verirsin?",
        cards: [
            { value: "low",    emoji: "😰", label: "Rahatsız olurum.",  desc: "1000 TL yatırdım, 950 TL görsem paniklerim." },
            { value: "medium", emoji: "😐", label: "Dayanırım.",        desc: "Kısa vadeli dalgalanma olabilir, anlıyorum." },
            { value: "high",   emoji: "😎", label: "Umrumda olmaz.",    desc: "Uzun vadeye bakıyorum, kısa düşüş fırsat olabilir." },
        ],
    },
];

// Anlık durum
let _currentStep  = 0;
let _profile      = {};   // { level, goal, riskTolerance }
let _onCompleteCb = null;

// ── Dışa Aktarılan Yardımcı ──────────────────────────────────────────────
export function getUserProfile() {
    try {
        const raw = localStorage.getItem(PROFILE_KEY);
        return raw ? JSON.parse(raw) : null;
    } catch { return null; }
}

// ── Ana Giriş Noktası ─────────────────────────────────────────────────────
export async function initOnboardingWizard(onCompleteCb) {
    _onCompleteCb = onCompleteCb;

    // 1. Önce Supabase'den kontrol et (source of truth)
    const serverResult = await _checkServerProfile();
    if (serverResult.is_onboarded && serverResult.onboarding_profile) {
        // Sunucu "tamamlandı" diyor → cache'e yaz, sihirbazı atla
        _saveToCache(serverResult.onboarding_profile);
        onCompleteCb(serverResult.onboarding_profile, false); // false: wizard was not shown
        return;
    }

    // 2. Sunucu erişilemezse localStorage cache'e düş
    const cached = getUserProfile();
    if (cached) {
        onCompleteCb(cached, false); // false: wizard was not shown
        return;
    }

    // 3. Yeni kullanıcı → sihirbazı başlat
    _currentStep = 0;
    _profile     = {};
    _showOverlay();
    _renderStep(0);
}

// ── Supabase Kontrol ──────────────────────────────────────────────────────
async function _checkServerProfile() {
    try {
        const session = await window.SupabaseAuth?.getValidSession?.();
        if (!session) return { is_onboarded: false, onboarding_profile: null };

        const resp = await fetch(`${API_BASE}/api/onboarding`, {
            headers: { "Authorization": `Bearer ${session.access_token}` }
        });
        if (resp.ok) return await resp.json();
    } catch (e) {
        console.warn("Onboarding profile fetch failed (offline fallback):", e);
    }
    return { is_onboarded: false, onboarding_profile: null };
}

// ── Overlay Göster / Gizle ───────────────────────────────────────────────
function _showOverlay() {
    const overlay = document.getElementById("onboarding-wizard-overlay");
    if (overlay) overlay.style.display = "flex";
}

function _hideOverlay() {
    const overlay = document.getElementById("onboarding-wizard-overlay");
    if (overlay) {
        overlay.style.opacity = "0";
        overlay.style.transition = "opacity 0.4s ease";
        setTimeout(() => { overlay.style.display = "none"; overlay.style.opacity = ""; }, 420);
    }
}

// ── Adım Render ───────────────────────────────────────────────────────────
function _renderStep(stepIndex) {
    const step = STEPS[stepIndex];

    const metaEl  = document.getElementById("wizard-step-meta");
    const titleEl = document.getElementById("wizard-step-title");
    const gridEl  = document.getElementById("wizard-cards-grid");

    if (!metaEl || !titleEl || !gridEl) return;

    // Metinleri güncelle
    metaEl.textContent  = step.meta;
    titleEl.textContent = step.title;

    // Kartları inşa et
    gridEl.innerHTML = "";
    gridEl.classList.remove("wizard-slide-in");
    // Tarayıcıya bir frame ver (reflow için)
    requestAnimationFrame(() => {
        step.cards.forEach(cardDef => {
            const card = _createCard(cardDef, step.key);
            gridEl.appendChild(card);
        });
        gridEl.classList.add("wizard-slide-in");
    });

    // İlerleme çubuğunu güncelle
    const progressFill = document.getElementById("wizard-progress-fill");
    if (progressFill) {
        progressFill.style.width = `${((stepIndex + 1) / STEPS.length) * 100}%`;
    }
}

function _createCard(cardDef, stepKey) {
    const card = document.createElement("div");
    card.className = "wizard-card";
    card.innerHTML = `
        <span class="wizard-card-emoji">${cardDef.emoji}</span>
        <strong class="wizard-card-label">${cardDef.label}</strong>
        <span class="wizard-card-desc">${cardDef.desc}</span>
    `;
    card.addEventListener("click", () => _selectCard(card, stepKey, cardDef.value));
    return card;
}

// ── Seçim & Geçiş ────────────────────────────────────────────────────────
function _selectCard(cardEl, key, value) {
    // Çift tıklamayı önle
    const grid = document.getElementById("wizard-cards-grid");
    if (grid && grid.dataset.locked === "true") return;
    if (grid) grid.dataset.locked = "true";

    // Seçim görselini uygula
    document.querySelectorAll(".wizard-card").forEach(c => c.classList.remove("selected"));
    cardEl.classList.add("selected");

    // Değeri kaydet
    _profile[key] = value;

    const nextStep = _currentStep + 1;

    // 380ms görsel geri bildirim, sonra geçiş
    setTimeout(() => {
        if (nextStep < STEPS.length) {
            _currentStep = nextStep;
            if (grid) grid.dataset.locked = "false";
            _renderStep(nextStep);
        } else {
            _completeWizard();
        }
    }, 380);
}

// ── Tamamla ───────────────────────────────────────────────────────────────
async function _completeWizard() {
    _saveToCache(_profile);
    _hideOverlay();

    // Supabase'e kaydet (başarısız olsa bile önce dashboard'u aç)
    _saveToServer(_profile).catch(e => console.warn("Onboarding save failed:", e));

    if (_onCompleteCb) _onCompleteCb(_profile, true); // true: wizard was shown
}

// ── Skip ──────────────────────────────────────────────────────────────────
export function skipWizard() {
    const defaultProfile = { level: "beginner", goal: "protect", riskTolerance: "low" };
    _saveToCache(defaultProfile);
    _saveToServer(defaultProfile).catch(e => console.warn("Onboarding skip save failed:", e));
    _hideOverlay();
    if (_onCompleteCb) _onCompleteCb(defaultProfile, true); // true: wizard was shown (and skipped)
}

// ── Supabase Kaydet ───────────────────────────────────────────────────────
async function _saveToServer(profile) {
    try {
        const session = await window.SupabaseAuth?.getValidSession?.();
        if (!session) return;

        await fetch(`${window.API_BASE}/api/onboarding`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "Authorization": `Bearer ${session.access_token}`
            },
            body: JSON.stringify({
                level:          profile.level,
                goal:           profile.goal,
                risk_tolerance: profile.riskTolerance,   // Backend snake_case bekliyor
            })
        });
    } catch (e) {
        console.warn("Onboarding server save failed:", e);
    }
}

// ── localStorage Cache ────────────────────────────────────────────────────
function _saveToCache(profile) {
    try { localStorage.setItem(PROFILE_KEY, JSON.stringify(profile)); }
    catch (e) { console.warn("Onboarding cache save failed:", e); }
}
