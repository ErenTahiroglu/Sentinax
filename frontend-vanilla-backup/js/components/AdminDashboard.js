export function initAdminDashboard() {
    const openBtn = document.getElementById("open-admin-btn");
    const closeBtn = document.getElementById("admin-modal-close");
    const modal = document.getElementById("admin-dashboard-modal");

    if (!openBtn || !modal) return;

    openBtn.addEventListener("click", async () => {
        modal.classList.remove("hidden");
        modal.style.display = "flex"; // Force display over toggle hidden
        await loadAdminMetrics();
    });

    closeBtn.addEventListener("click", () => {
        modal.style.display = "none";
        modal.classList.add("hidden");
    });

    // Close on outside click
    modal.addEventListener("click", (e) => {
        if (e.target === modal) {
             modal.style.display = "none";
             modal.classList.add("hidden");
        }
    });

    // Custom condition trigger to reveal sidebar section
    checkAdminAccess();
}

async function checkAdminAccess() {
    const adminSection = document.getElementById("admin-sidebar-section");
    if (!adminSection) return;

    // 1. Check for manual bypass (Development/Debug Mode)
    if (localStorage.getItem("admin_bypass") === "true") {
        adminSection.classList.remove("hidden");
        return;
    }

    if (!window.SupabaseAuth) return;
    try {
        const session = await window.SupabaseAuth.getValidSession();
        if (!session || !session.access_token) return;
        
        const token = session.access_token;
        // Call the endpoint, if 200, reveal button!
        const resp = await fetch(`${window.API_BASE}/api/admin/metrics`, {
            headers: { "Authorization": `Bearer ${token}` }
        });
        
        if (resp.ok) {
            adminSection.classList.remove("hidden");
        }
    } catch (e) {
         // Silently fail if not admin
    }
}

async function loadAdminMetrics() {
    if (!window.SupabaseAuth) return;
    try {
        const session = await window.SupabaseAuth.getValidSession();
        if (!session) return;
        const token = session.access_token;

        const resp = await fetch(`${window.API_BASE}/api/admin/metrics`, {
            headers: { "Authorization": `Bearer ${token}` }
        });
        
        if (!resp.ok) throw new Error("Metrics load failed");
        const data = await resp.json();

        // Populate metrics
        const elTotalUsers = document.getElementById("admin-total-users");
        if (elTotalUsers) elTotalUsers.textContent = data.total_users || 0;

        const elAum = document.getElementById("admin-total-aum");
        if (elAum) elAum.textContent = data.aum ? "$" + data.aum.toLocaleString() : "$0";

        const elCost = document.getElementById("admin-llm-cost");
        if (elCost) elCost.textContent = data.cost_24h ? "$" + data.cost_24h.toFixed(6) : "$0";

        // Top Users List
        const topUsersList = document.getElementById("admin-top-users");
        if (topUsersList) {
            if (data.top_users && data.top_users.length > 0) {
                topUsersList.innerHTML = data.top_users.map((u, i) => `
                    <li style="display: flex; justify-content: space-between; align-items: center; background: rgba(255,255,255,0.02); padding: 8px 12px; border-radius: 6px; border: 1px solid rgba(255,255,255,0.05);">
                        <div style="display: flex; align-items: center; gap: 8px;">
                            <span style="font-weight: 700; color: #e74c3c;">#${i+1}</span>
                            <span style="font-size: 0.8rem; color: var(--text-muted); font-family: monospace;">${u.user_id.split('-')[0]}..</span>
                        </div>
                        <span style="background: rgba(231, 76, 60, 0.1); color: #e74c3c; font-size: 0.75rem; padding: 2px 8px; border-radius: 4px; font-weight: 600;">${u.usage_count} Requests</span>
                    </li>
                `).join('');
            } else {
                topUsersList.innerHTML = `<li style="color: var(--text-muted); font-style: italic;">No usage logged.</li>`;
            }
        }

        // Render Cost Chart using Lightweight Charts
        const chartContainer = document.getElementById("admin-cost-chart");
        if (chartContainer && data.chart_data && data.chart_data.length > 0) {
            chartContainer.innerHTML = ''; // Clear prior if any
            
            const chart = LightweightCharts.createChart(chartContainer, {
                width: chartContainer.clientWidth,
                height: 250,
                layout: { background: { color: 'transparent' }, textColor: '#b2b5be' },
                grid: { vertLines: { color: 'rgba(42, 46, 57, 0.1)' }, horzLines: { color: 'rgba(42, 46, 57, 0.1)' } },
                rightPriceScale: { borderVisible: false },
                timeScale: { borderVisible: false }
            });

            const areaSeries = chart.addAreaSeries({
                topColor: 'rgba(231, 76, 60, 0.2)',
                bottomColor: 'rgba(231, 76, 60, 0.0)',
                lineColor: '#e74c3c',
                lineWidth: 2,
            });

            const chartPoints = data.chart_data.map(d => ({
                time: d.date,
                value: d.cost
            }));

            areaSeries.setData(chartPoints);
            chart.timeScale().fitContent();

            // Handle resize
            const resizeObserver = new ResizeObserver(() => {
                chart.resize(chartContainer.clientWidth, 250);
            });
            resizeObserver.observe(chartContainer);
        } else if (chartContainer) {
            chartContainer.innerHTML = `<div style="display: flex; align-items: center; justify-content: center; height: 100%; color: var(--text-muted);">No chart data available.</div>`;
        }

    } catch (e) {
        console.error("Admin Metrics load failed:", e);
    }
}
