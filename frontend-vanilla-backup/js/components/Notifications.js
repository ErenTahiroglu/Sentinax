// ═══════════════════════════════════════
// NOTIFICATION & ALERTS MODULE
// ═══════════════════════════════════════

export function toggleNotifications() {
    const dropdown = document.getElementById("notification-dropdown");
    if (dropdown) {
        if (dropdown.style.display === "none" || dropdown.classList.contains("hidden")) {
            dropdown.style.display = "flex";
            dropdown.classList.remove("hidden");
        } else {
            dropdown.style.display = "none";
            dropdown.classList.add("hidden");
        }
    }
}

export async function markAllAlertsRead() {
    try {
        const session = await window.SupabaseAuth.getValidSession();
        if (!session) return;

        await fetch(`${window.API_BASE}/api/alerts/read`, {
            method: "POST",
            headers: { 
                "Authorization": `Bearer ${session.access_token}`,
                "Content-Type": "application/json"
            },
            body: JSON.stringify({}) 
        });
        
        const badge = document.getElementById("notification-badge");
        if (badge) badge.style.display = "none";
        const list = document.getElementById("notification-list");
        if (list) list.innerHTML = "Tüm bildirimler temizlendi.";
    } catch(e) {
        console.warn("Mark read failed:", e);
    }
}

export async function fetchAutonomousAlerts() {
    const container = document.getElementById("notification-container");
    const badge = document.getElementById("notification-badge");
    const list = document.getElementById("notification-list");
    if (!container || !list) return;

    try {
        const session = await window.SupabaseAuth.getValidSession();
        if (!session) return;

        const res = await fetch(`${window.API_BASE}/api/alerts`, {
            headers: { "Authorization": `Bearer ${session.access_token}` }
        });
        
        if (!res.ok) return;
        const alerts = await res.json();
        
        container.style.display = "block"; 
        
        if (alerts.length > 0) {
            badge.style.display = "inline";
            badge.innerText = alerts.length;
            
            list.innerHTML = alerts.map(a => `
                <div style="padding: 8px; border-bottom: 1px solid rgba(255,255,255,0.05); margin-bottom: 4px;">
                    <div style="font-size: 0.70rem; color: var(--primary); margin-bottom: 4px;">
                        <i class="fas fa-clock"></i> ${new Date(a.created_at).toLocaleDateString('tr-TR', {hour:'2-digit', minute:'2-digit'})}
                    </div>
                    <strong>${a.ticker}</strong> <span style="font-size:0.8rem;">${a.message}</span>
                </div>
            `).join("");
        } else {
            badge.style.display = "none";
            list.innerHTML = "Piyasa sakin. Yeni uyarı yok.";
        }
    } catch (e) {
        console.warn("Alert fetch failed:", e);
    }
}
