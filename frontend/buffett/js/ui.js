function renderPortfolioGrid(portfolio) {
    const grid = document.getElementById('portfolio-grid');
    grid.innerHTML = '';

    portfolio.forEach(stock => {
        const card = document.createElement('div');
        card.className = 'stock-card';
        
        // Gauge percentage
        const scorePct = Math.min(100, Math.max(0, stock.total_score));
        
        card.innerHTML = `
            <div class="card-header">
                <div>
                    <span class="ticker">${stock.ticker}</span>
                    <span class="company-name">${stock.name}</span>
                </div>
                <div class="score-badge">${stock.total_score.toFixed(1)}</div>
            </div>
            
            <div class="card-metrics">
                <div class="metric-row">
                    <span class="metric-label">Current Price</span>
                    <span class="metric-value">₺${stock.current_price.toFixed(2)}</span>
                </div>
                <div class="metric-row">
                    <span class="metric-label">Target Entry</span>
                    <span class="metric-value">₺${stock.target_entry[0]} - ₺${stock.target_entry[1]}</span>
                </div>
                <div class="metric-row">
                    <span class="metric-label">Margin of Safety</span>
                    <span class="metric-value ${stock.margin_of_safety_pct > 0 ? 'value-success' : 'value-danger'}">${stock.margin_of_safety_pct.toFixed(2)}%</span>
                </div>
                
                <div style="margin-top: 10px">
                    <div style="display:flex; justify-content:space-between; font-size:0.85rem; color:var(--text-secondary); margin-bottom:5px;">
                        <span>Buffett Score</span>
                        <span>${scorePct.toFixed(1)}/100</span>
                    </div>
                    <div class="score-bar-bg">
                        <div class="score-bar-fill" style="width: ${scorePct}%"></div>
                    </div>
                </div>
            </div>
        `;

        card.addEventListener('click', () => openDetailDrawer(stock));
        grid.appendChild(card);
    });
}

function openDetailDrawer(stock) {
    const drawer = document.getElementById('detail-drawer');
    const overlay = document.getElementById('drawer-overlay');
    const content = document.getElementById('drawer-content');

    const details = stock.details;
    
    content.innerHTML = `
        <div class="drawer-header">
            <h2>${stock.ticker}</h2>
            <p>${stock.name}</p>
        </div>
        
        <div class="drawer-section">
            <h3>Alt Skorlar (Moat & Profitability)</h3>
            <div class="alt-scores">
                <div class="score-item">
                    <span>Moat</span>
                    <span>${details.moat_score}/20</span>
                </div>
                <div class="score-item">
                    <span>Profitability</span>
                    <span>${details.profitability_score}/30</span>
                </div>
                <div class="score-item">
                    <span>Balance Sheet</span>
                    <span>${details.balance_sheet_score}/20</span>
                </div>
                <div class="score-item">
                    <span>Valuation (MoS)</span>
                    <span>${details.valuation_score}/30</span>
                </div>
            </div>
        </div>
        
        <div class="drawer-section">
            <h3>Değerleme (DCF Senaryoları)</h3>
            <div class="alt-scores">
                <div class="score-item">
                    <span>Base Case</span>
                    <span>₺${details.dcf.base_case}</span>
                </div>
                <div class="score-item">
                    <span>Bear Case</span>
                    <span>₺${details.dcf.bear_case}</span>
                </div>
                <div class="score-item">
                    <span>Bull Case</span>
                    <span>₺${details.dcf.bull_case}</span>
                </div>
                <div class="score-item" style="margin-top: 10px; border-top: 1px solid rgba(255,255,255,0.1); padding-top: 10px;">
                    <span style="color:var(--accent)">Intrinsic Value (Avg)</span>
                    <span style="font-weight:bold; color:var(--accent)">₺${details.dcf.intrinsic_value}</span>
                </div>
            </div>
        </div>
        
        <div class="drawer-section">
            <h3>Nitel Analiz (LLM)</h3>
            <p class="llm-analysis">"${details.llm_analysis}"</p>
        </div>
        
        <div class="drawer-section">
            <h3>Veto Durumu</h3>
            <p style="color: ${details.passed_veto ? 'var(--success)' : 'var(--danger)'}; font-weight: 600;">
                ${details.passed_veto ? '✓ Veto testlerinden başarıyla geçti.' : '✗ Veto testlerine takıldı.'}
            </p>
        </div>
    `;

    drawer.classList.remove('hidden');
    overlay.classList.remove('hidden');
}

function closeDetailDrawer() {
    document.getElementById('detail-drawer').classList.add('hidden');
    document.getElementById('drawer-overlay').classList.add('hidden');
}

document.getElementById('close-drawer').addEventListener('click', closeDetailDrawer);
document.getElementById('drawer-overlay').addEventListener('click', closeDetailDrawer);

function renderHistoryList(changes) {
    const list = document.getElementById('history-list');
    list.innerHTML = '';
    
    if (changes.length === 0) {
        list.innerHTML = '<p>No recent portfolio changes.</p>';
        return;
    }

    changes.forEach(change => {
        const isAdded = change.action === 'ADDED';
        const el = document.createElement('div');
        el.className = 'history-item';
        
        el.innerHTML = `
            <div class="history-badge ${isAdded ? 'badge-added' : 'badge-removed'}">
                ${change.action}
            </div>
            <div class="history-content">
                <h4>${change.ticker}</h4>
                <p>${change.reason}</p>
            </div>
            <div class="history-date">
                ${change.date}
            </div>
        `;
        
        list.appendChild(el);
    });
}

window.ui = {
    renderPortfolioGrid,
    renderHistoryList
};
