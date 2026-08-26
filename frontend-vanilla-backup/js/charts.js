// ═══════════════════════════════════════
// CHART HELPERS
// ═══════════════════════════════════════
let tvChartInstances = {};
let chartInstances = {};

function destroyChart(id) {
    if (chartInstances[id]) { chartInstances[id].destroy(); delete chartInstances[id]; }
}

function getChartColors() {
    const isDark = document.documentElement.getAttribute("data-theme") !== "light" || (!localStorage.getItem("theme") && window.matchMedia("(prefers-color-scheme: dark)").matches);
    return {
        grid: isDark ? "rgba(148,163,184,0.1)" : "rgba(0,0,0,0.06)",
        text: isDark ? "#94a3b8" : "#64748b",
    };
}

// ═══════════════════════════════════════
// TRADINGVIEW LIGHTWEIGHT CHARTS
// ═══════════════════════════════════════
window.createTVChart = createTVChart;
function createTVChart(containerId, res) {
    const container = document.getElementById(containerId);
    if (!container) return;

    // Önceki içeriği temizle (Bellek sızıntısı önlemi: instance'ı güvenle .remove() ile yok et)
    if (tvChartInstances[containerId]) {
        try { 
            if (tvChartInstances[containerId]._resizeObserver) {
                tvChartInstances[containerId]._resizeObserver.disconnect();
            }
            tvChartInstances[containerId].remove(); 
        } catch (e) { /* ignore */ }
        delete tvChartInstances[containerId];
    }
    container.innerHTML = "";

    const isDark = document.documentElement.getAttribute("data-theme") !== "light" || (!localStorage.getItem("theme") && window.matchMedia("(prefers-color-scheme: dark)").matches);

    const chartOptions = {
        layout: {
            background: { type: 'solid', color: 'transparent' },
            textColor: isDark ? '#94a3b8' : '#64748b',
        },
        grid: {
            vertLines: { color: isDark ? 'rgba(148,163,184,0.05)' : 'rgba(0,0,0,0.04)' },
            horzLines: { color: isDark ? 'rgba(148,163,184,0.05)' : 'rgba(0,0,0,0.04)' },
        },
        crosshair: {
            mode: 1, // Normal
        },
        rightPriceScale: {
            borderVisible: false,
        },
        timeScale: {
            borderVisible: false,
            timeVisible: true,
        },
        handleScroll: true,
        handleScale: true,
    };

    const chart = LightweightCharts.createChart(container, chartOptions);
    tvChartInstances[containerId] = chart;
    
    // Resize Observer
    const resizeObserver = new ResizeObserver(entries => {
        if (entries[0].contentRect.width > 0) {
            chart.applyOptions({ width: entries[0].contentRect.width, height: container.clientHeight || 250 });
        }
    });
    resizeObserver.observe(container);
    chart._resizeObserver = resizeObserver;

    // 1. Mum Grafiği (Kripto veya Zengin Veri)
    if (res.klines && res.klines.length > 0) {
        const candleSeries = chart.addCandlestickSeries({
            upColor: '#22c55e', downColor: '#ef4444', borderVisible: false,
            wickUpColor: '#22c55e', wickDownColor: '#ef4444',
        });
        candleSeries.setData(res.klines);

        // Hacim Barı (Volume)
        const volumeSeries = chart.addHistogramSeries({
            color: '#38bdf8',
            priceFormat: { type: 'volume' },
            priceScaleId: '', // Ayrı skala
        });
        volumeSeries.priceScale().applyOptions({
            scaleMargins: { top: 0.8, bottom: 0 }, // Alt %20 kısmında göster
        });
        
        const volumeData = res.klines.map(k => ({
            time: k.time,
            value: k.volume,
            color: k.close >= k.open ? 'rgba(34,197,94,0.4)' : 'rgba(239,68,68,0.4)'
        }));
        volumeSeries.setData(volumeData);
    } 
    // 2. Alan Grafiği (Area Series) - Klasik Veriler
    else if (res.financials && res.financials.yg) {
        const areaSeries = chart.addAreaSeries({
            lineColor: '#0ea5e9',
            topColor: 'rgba(14, 165, 233, 0.4)',
            bottomColor: 'rgba(14, 165, 233, 0.0)',
            lineWidth: 2,
        });

        // Veriyi Zaman Serisine Dönüştür
        const data = Object.entries(res.financials.yg)
            .sort((a, b) => new Date(a[0]) - new Date(b[0]))
            .map(([dateStr, val]) => {
                const cur_t = Math.floor(new Date(dateStr).getTime() / 1000);
                return { time: cur_t, value: parseFloat(val) };
            })
            .filter(d => !isNaN(d.time) && !isNaN(d.value));

        if (data.length > 0) {
            areaSeries.setData(data);
        }
    }

    chart.timeScale().fitContent();
}

function createBacktestChart(containerId, simData) {
    const container = document.getElementById(containerId);
    if (!container || !simData || !simData.dates || simData.dates.length === 0) return;

    // Önceki içeriği temizle (Bellek sızıntısı önlemi: instance'ı güvenle .remove() ile yok et)
    if (tvChartInstances[containerId]) {
        try { 
            if (tvChartInstances[containerId]._resizeObserver) {
                tvChartInstances[containerId]._resizeObserver.disconnect();
            }
            tvChartInstances[containerId].remove(); 
        } catch (e) { /* ignore */ }
        delete tvChartInstances[containerId];
    }
    container.innerHTML = "";

    const isDark = document.documentElement.getAttribute("data-theme") !== "light" || (!localStorage.getItem("theme") && window.matchMedia("(prefers-color-scheme: dark)").matches);

    const chartOptions = {
        layout: {
            background: { type: 'solid', color: 'transparent' },
            textColor: isDark ? '#94a3b8' : '#64748b',
        },
        grid: {
            vertLines: { color: isDark ? 'rgba(148,163,184,0.05)' : 'rgba(0,0,0,0.04)' },
            horzLines: { color: isDark ? 'rgba(148,163,184,0.05)' : 'rgba(0,0,0,0.04)' },
        },
        crosshair: { mode: 1 },
        rightPriceScale: { borderVisible: false },
        timeScale: { borderVisible: false, timeVisible: true },
        handleScroll: true,
        handleScale: true,
    };

    const chart = LightweightCharts.createChart(container, chartOptions);
    tvChartInstances[containerId] = chart;

    const resizeObserver = new ResizeObserver(entries => {
        if (entries[0].contentRect.width > 0) {
            chart.applyOptions({ width: entries[0].contentRect.width, height: container.clientHeight || 350 });
        }
    });
    resizeObserver.observe(container);
    chart._resizeObserver = resizeObserver;

    // 1. Portföy Büyümesi (Alan)
    const portfolioSeries = chart.addAreaSeries({
        lineColor: '#0ea5e9',
        topColor: 'rgba(14, 165, 233, 0.4)',
        bottomColor: 'rgba(14, 165, 233, 0.0)',
        lineWidth: 3,
        title: 'Portföy',
    });

    // 2. Benchmark Büyümesi (Çizgi)
    const benchmarkSeries = chart.addLineSeries({
        color: '#f43f5e',
        lineWidth: 2,
        lineStyle: 1, // 0:Solid, 1:Dotted
        title: 'Blended Index',
    });

    const portfolioData = [];
    const benchmarkData = [];

    for (let i = 0; i < simData.dates.length; i++) {
        const timeVal = Math.floor(new Date(simData.dates[i]).getTime() / 1000);
        if (isNaN(timeVal)) continue;

        if (simData.balance_history && simData.balance_history[i] !== undefined) {
             portfolioData.push({ time: timeVal, value: simData.balance_history[i] });
        }

        if (simData.benchmark_history && simData.benchmark_history[i] !== undefined) {
             benchmarkData.push({ time: timeVal, value: simData.benchmark_history[i] });
        }
    }

    portfolioData.sort((a, b) => a.time - b.time);
    benchmarkData.sort((a, b) => a.time - b.time);

    if (portfolioData.length > 0) portfolioSeries.setData(portfolioData);
    if (benchmarkData.length > 0) benchmarkSeries.setData(benchmarkData);

    chart.timeScale().fitContent();
    updateBacktestMetrics(simData);
}

function updateBacktestMetrics(simData) {
    if (!simData || !simData.metrics) return;

    const finalBalanceEl = document.getElementById("bt-final-balance");
    const cagrEl = document.getElementById("bt-cagr");
    const maxDdEl = document.getElementById("bt-max-dd");
    const sharpeEl = document.getElementById("bt-sharpe");

    if (finalBalanceEl) finalBalanceEl.innerText = simData.final_balance ? `${simData.final_balance.toLocaleString("tr-TR")} ₺/$` : "-";
    if (cagrEl) cagrEl.innerText = simData.metrics.cagr !== undefined ? `%${simData.metrics.cagr.toFixed(1)}` : "-";
    if (maxDdEl) maxDdEl.innerText = simData.metrics.max_drawdown !== undefined ? `-%${Math.abs(simData.metrics.max_drawdown).toFixed(1)}` : "-";
    if (sharpeEl) sharpeEl.innerText = simData.metrics.sharpe !== undefined ? simData.metrics.sharpe.toFixed(2) : "-";
}

window.createReturnChart = createReturnChart;
function createReturnChart(canvasId, fin) {
    const canvas = document.getElementById(canvasId);
    if (!canvas || !fin) return;
    const years = Object.keys(fin.yg || {}).sort();
    if (years.length === 0) return;
    const { grid, text } = getChartColors();
    destroyChart(canvasId);
    chartInstances[canvasId] = new Chart(canvas, {
        type: "line",
        data: {
            labels: years,
            datasets: [
                { type: "bar", label: "Nominal (%)", data: years.map(y => fin.yg[y]), backgroundColor: years.map(y => fin.yg[y] >= 0 ? "rgba(99,102,241,0.7)" : "rgba(239,68,68,0.6)"), borderRadius: 4, barPercentage: 0.7 },
                { type: "line", label: "Reel (%)", data: years.map(y => fin.yr[y]), backgroundColor: "rgba(56,189,248,0.2)", borderColor: "rgba(56,189,248,1)", borderWidth: 2, fill: true, tension: 0.4, pointRadius: 3, pointHoverRadius: 6 },
            ],
        },
        options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: "top", labels: { color: text, font: { size: 11, family: "Inter" }, boxWidth: 12 } } }, scales: { x: { grid: { display: false }, ticks: { color: text, font: { size: 11 } } }, y: { grid: { color: grid }, ticks: { color: text, font: { size: 11 }, callback: v => v + "%" } } } },
    });
}

// ═══════════════════════════════════════
// TECHNICAL INDICATORS RENDERER
// ═══════════════════════════════════════
// renderTechnicals has been moved to components/TechnicalsComponent.js

// ═══════════════════════════════════════
// RENDER PORTFOLIO EXTRAS
// ═══════════════════════════════════════
window.renderExtras = renderExtras;
function renderExtras(extras) {
    if (!extras) return;

    // Sector Distribution
    const sectorCard = document.getElementById("sector-card");
    if (extras.sector_distribution && Object.keys(extras.sector_distribution).length > 0) {
        sectorCard.classList.remove("hidden");
        const sectors = extras.sector_distribution;
        const labels = Object.keys(sectors);
        const values = Object.values(sectors);
        const colors = ["#6366f1", "#38bdf8", "#22c55e", "#f59e0b", "#ef4444", "#a855f7", "#ec4899", "#14b8a6", "#f97316", "#84cc16"];
        destroyChart("sector-chart");
        chartInstances["sector-chart"] = new Chart(document.getElementById("sector-chart"), {
            type: "doughnut",
            data: { labels, datasets: [{ data: values, backgroundColor: colors.slice(0, labels.length), borderWidth: 0 }] },
            options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: "right", labels: { color: getChartColors().text, font: { size: 11, family: "Inter" }, boxWidth: 12, padding: 8 } } } },
        });
    } else { sectorCard.classList.add("hidden"); }

    // Correlation Matrix
    const corrCard = document.getElementById("correlation-card");
    if (extras.correlation && extras.correlation.tickers && extras.correlation.tickers.length >= 2) {
        corrCard.classList.remove("hidden");
        const { tickers, matrix } = extras.correlation;
        let html = `<table class="correlation-table"><thead><tr><th></th>`;
        tickers.forEach(t => { html += `<th>${t}</th>`; });
        html += `</tr></thead><tbody>`;
        matrix.forEach((row, i) => {
            html += `<tr><th>${tickers[i]}</th>`;
            row.forEach(v => {
                const hue = v > 0 ? 130 : 0;
                const lightness = 100 - Math.abs(v) * 40;
                const color = `hsl(${hue}, 70%, ${lightness}%)`;
                html += `<td class="corr-cell" style="background:${color};color:${lightness < 60 ? '#fff' : '#000'}">${v.toFixed(2)}</td>`;
            });
            html += `</tr>`;
        });
        html += `</tbody></table>`;
        document.getElementById("correlation-content").innerHTML = html;
    } else { corrCard.classList.add("hidden"); }

    // Monte Carlo
    const mcCard = document.getElementById("monte-carlo-card");
    if (extras.monte_carlo) {
        mcCard.classList.remove("hidden");
        const mc = extras.monte_carlo;
        const labels = mc.months.map(m => `${m}ay`);
        const { grid, text } = getChartColors();
        destroyChart("monte-carlo-chart");
        chartInstances["monte-carlo-chart"] = new Chart(document.getElementById("monte-carlo-chart"), {
            type: "line",
            data: {
                labels,
                datasets: [
                    { label: "%5", data: mc.percentiles.p5, borderColor: "rgba(239,68,68,0.5)", backgroundColor: "transparent", borderWidth: 1, pointRadius: 0, borderDash: [4, 2] },
                    { label: "%25", data: mc.percentiles.p25, borderColor: "rgba(245,158,11,0.5)", backgroundColor: "transparent", borderWidth: 1, pointRadius: 0, borderDash: [4, 2] },
                    { label: "%50 (Medyan)", data: mc.percentiles.p50, borderColor: "#6366f1", backgroundColor: "rgba(99,102,241,0.1)", borderWidth: 2.5, pointRadius: 0, fill: false },
                    { label: "%75", data: mc.percentiles.p75, borderColor: "rgba(56,189,248,0.5)", backgroundColor: "transparent", borderWidth: 1, pointRadius: 0, borderDash: [4, 2] },
                    { label: "%95", data: mc.percentiles.p95, borderColor: "rgba(34,197,94,0.5)", backgroundColor: "transparent", borderWidth: 1, pointRadius: 0, borderDash: [4, 2] },
                ],
            },
            options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: "top", labels: { color: text, font: { size: 10, family: "Inter" }, boxWidth: 10 } } }, scales: { x: { grid: { display: false }, ticks: { color: text, font: { size: 10 } } }, y: { grid: { color: grid }, ticks: { color: text, font: { size: 10 }, callback: v => (v * 100 - 100).toFixed(0) + "%" } } } },
        });
    } else if (mcCard) {
        mcCard.classList.remove("hidden");
        document.getElementById("monte-carlo-chart").innerHTML = "<div style='color:var(--text-muted); text-align:center; padding:2rem; font-size: 0.85rem;'>Monte Carlo simülasyonu oluşturulamadı veya veri eksik.</div>";
    }

    // PV Simulation
    const pvWrap = document.getElementById("pv-simulation-wrap");
    if (pvWrap && extras.pv_simulation && Object.keys(extras.pv_simulation).length > 0) {
        pvWrap.classList.remove("hidden");
        const sim = extras.pv_simulation;
        const met = sim.metrics || {};
        
        document.getElementById("pv-cagr").textContent = met.cagr !== undefined ? `%${met.cagr}` : "-";
        document.getElementById("pv-balance").textContent = sim.final_balance ? `${sim.final_balance.toLocaleString('tr-TR')} ₺` : "-";
        document.getElementById("pv-maxdd").textContent = met.max_drawdown !== undefined ? `-%${Math.abs(met.max_drawdown)}` : "-";
        document.getElementById("pv-sortino").textContent = met.sortino !== undefined ? met.sortino : "-";
        document.getElementById("pv-calmar").textContent = met.calmar !== undefined ? met.calmar : "-";
        document.getElementById("pv-sharpe").textContent = met.sharpe !== undefined ? met.sharpe : "-";
        
        if (met.drawdown_series && met.drawdown_series.length > 0) {
            destroyChart("drawdown-chart");
            const labels = Array.from({length: met.drawdown_series.length}, (_, i) => `${i+1}A`);
            chartInstances["drawdown-chart"] = new Chart(document.getElementById("drawdown-chart"), {
                type: "line",
                data: {
                    labels: labels,
                    datasets: [{
                        label: "Drawdown (%)",
                        data: met.drawdown_series,
                        borderColor: "rgba(239, 68, 68, 0.8)",
                        backgroundColor: "rgba(239, 68, 68, 0.2)",
                        fill: true,
                        tension: 0.3,
                        pointRadius: 0
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: { legend: { display: false } },
                    scales: {
                        x: { display: false },
                        y: { 
                            grid: { color: getChartColors().grid },
                            ticks: { color: getChartColors().text, callback: v => v + "%" },
                            max: 0
                        }
                    }
                }
            });
        }
    } else if (pvWrap) {
        pvWrap.classList.add("hidden");
    }

    // New Advanced Backtest (Phase 1)
    if (extras.pv_simulation) {
        try {
            createBacktestChart("bt-chart-container", extras.pv_simulation);
        } catch (e) {
            console.error("Backtest Chart error:", e);
        }
    }

    // Factor Regression
    const factorContent = document.getElementById("pv-factor-content");
    if (factorContent) {
        if (extras.factor_regression) {
            const f = extras.factor_regression;
            if (f.error) {
                factorContent.innerHTML = `<div class="pv-warning"><i class="fas fa-exclamation-triangle"></i> ${f.message}</div>`;
            } else {
                factorContent.innerHTML = `
                    <div class="factor-row"><span>Yıllık Alpha</span><strong style="color:var(--success)">%${f.alpha_annual}</strong></div>
                    <div class="factor-row"><span>Piyasa (Market) Beta</span><strong>${f.market_beta}</strong></div>
                    <div class="factor-row"><span>Ölçek (Size) Beta</span><strong>${f.size_beta}</strong></div>
                    <div class="factor-row"><span>Değer (Value) Beta</span><strong>${f.value_beta}</strong></div>
                `;
            }
        } else {
            factorContent.innerHTML = `<div style="color:var(--text-muted); font-size:0.8rem;">Bu portföy için faktör verisi oluşturulamadı.</div>`;
        }
    }
}

// ═══════════════════════════════════════
// HEATMAP (TREEMAP)
// ═══════════════════════════════════════
// renderHeatmap has been moved to components/HeatmapComponent.js

// ═══════════════════════════════════════
// SCENARIOS & DIVIDEND CALCULATOR
// ═══════════════════════════════════════
window.renderScenarios = renderScenarios;
function renderScenarios(results) {
    const wrap = document.getElementById("scenarios-wrap");
    if (!results || results.length === 0) {
        wrap.classList.add("hidden");
        return;
    }
    wrap.classList.remove("hidden");

    let totalWeight = 0;
    let weightedBeta = 0;
    let weightedDiv = 0;

    results.forEach(r => {
        if (!r.error && r.financials && r.valuation) {
            const w = r.weight || 1;
            totalWeight += w;
            const beta = r.valuation.beta || 1;
            const div = r.valuation.div_yield || 0;
            weightedBeta += beta * w;
            weightedDiv += div * w;
        }
    });

    const avgBeta = totalWeight > 0 ? weightedBeta / totalWeight : 1;
    const avgDivYield = totalWeight > 0 ? weightedDiv / totalWeight : 0;

    // Update UI Indicators
    document.getElementById("portfolio-beta-val").textContent = avgBeta.toFixed(2);
    document.getElementById("avg-div-yield-val").textContent = `%${avgDivYield.toFixed(2)}`;

    // Stress Test Logic
    const gaugeFill = document.getElementById("shock-gauge-fill");
    const resultVal = document.getElementById("scenario-stress-result-val");
    const resultName = document.getElementById("scenario-stress-name");

    const btns = {
        '2008': document.getElementById("btn-scenario-2008"),
        'covid': document.getElementById("btn-scenario-covid"),
        'tech': document.getElementById("btn-scenario-tech")
    };

    function updateStressUI(dropPct, name, btnKey) {
        const expectedDrop = dropPct * avgBeta;
        resultVal.textContent = `-%${expectedDrop.toFixed(1)}`;
        resultName.innerHTML = `${name}<div style="font-size:0.7rem; color:var(--text-muted); font-weight:normal; margin-top:0.2rem;">Piyasa Şoku: -%${dropPct} | Portföy Etkisi</div>`;

        // Gauge rotation: -45deg is 0%, 135deg is 100% (range of 180deg)
        const rotation = -45 + (Math.min(expectedDrop, 100) * 1.8);
        gaugeFill.style.transform = `rotate(${rotation}deg)`;

        // Set active button
        Object.values(btns).forEach(b => b?.classList.remove("active"));
        btns[btnKey]?.classList.add("active");
    }

    if (btns['2008']) btns['2008'].onclick = () => updateStressUI(50, "2008 Krizi", '2008');
    if (btns['covid']) btns['covid'].onclick = () => updateStressUI(33, "Covid-19", 'covid');

    // Tech Crash: Simulated 40% drop in high-beta tech stocks.
    if (btns['tech']) btns['tech'].onclick = () => updateStressUI(40, "Tech Crash", 'tech');

    // Default view
    updateStressUI(50, "2008 Krizi", '2008');

    // Dividend FI/RE Calculator
    const inMonthlyAdd = document.getElementById("div-monthly-add");
    const inTargetIncome = document.getElementById("div-target-income");
    const resYears = document.getElementById("fire-years");
    const resCapital = document.getElementById("fire-capital");

    let userInteracted = false;

    function calcDivFIRE() {
        if (!userInteracted) {
            resYears.textContent = "---";
            resCapital.textContent = "---";
            return;
        }

        const P = parseFloat(inMonthlyAdd.value);
        const targetMonthly = parseFloat(inTargetIncome.value);

        if (avgDivYield <= 0.1 || !P || !targetMonthly || P <= 0 || targetMonthly <= 0) {
            resYears.textContent = "---";
            resCapital.textContent = "---";
            return;
        }

        const r = (avgDivYield / 100) / 12;
        const targetCap = targetMonthly / r;
        resCapital.textContent = targetCap.toLocaleString('tr-TR', { maximumFractionDigits: 0 }) + " ₺";

        if (r > 0) {
            const months = Math.log(1 + (targetCap * r) / P) / Math.log(1 + r);
            resYears.textContent = (months / 12).toFixed(1);
        } else {
            resYears.textContent = "∞";
        }
    }

    inMonthlyAdd.oninput = () => { userInteracted = true; calcDivFIRE(); };
    inTargetIncome.oninput = () => { userInteracted = true; calcDivFIRE(); };

    // Initial call (will show --- because userInteracted is false)
    calcDivFIRE();
}

// ═══════════════════════════════════════
// OPTIMIZATION
// ═══════════════════════════════════════
window.renderOptimization = renderOptimization;
function renderOptimization(optGroups, results) {
    const wrap = document.getElementById("optimization-wrap");
    const container = document.getElementById("opt-bars");

    if (!optGroups || Object.keys(optGroups).length === 0 || !optGroups.max_sharpe) {
        wrap.classList.add("hidden");
        return;
    }

    wrap.classList.remove("hidden");
    container.innerHTML = "";
    
    let html = `<div class="opt-tabs" style="display:flex; gap:0.5rem; margin-bottom:1rem; border-bottom:1px solid var(--glass-border); padding-bottom:0.5rem;">
        <button class="btn btn-outline active" onclick="switchOptTab('max_sharpe', this)" style="font-size:0.75rem; padding:0.3rem 0.6rem;">Maksimum Sharpe</button>
        <button class="btn btn-outline" onclick="switchOptTab('min_volatility', this)" style="font-size:0.75rem; padding:0.3rem 0.6rem;">Minimum Risk</button>
        <button class="btn btn-outline" onclick="switchOptTab('max_return', this)" style="font-size:0.75rem; padding:0.3rem 0.6rem;">Maksimum Getiri</button>
    </div>`;
    
    window.currentOptGroups = optGroups;
    window.currentOptResults = results;
    
    html += `<div id="opt-tab-content"></div>`;
    container.innerHTML = html;
    
    // Initially render
    if (typeof window.switchOptTab === "function") window.switchOptTab('max_sharpe', container.querySelector('.opt-tabs button'));
}

window.switchOptTab = function(type, btnObj) {
    if(btnObj) {
        document.querySelectorAll('.opt-tabs button').forEach(b => {
             b.classList.remove('active');
             b.style.background = 'transparent';
             b.style.color = 'var(--text-main)';
        });
        btnObj.classList.add('active');
        btnObj.style.background = 'var(--primary-glow)';
        btnObj.style.color = 'var(--primary)';
    }
    const optWeights = window.currentOptGroups[type];
    const results = window.currentOptResults;
    const content = document.getElementById("opt-tab-content");
    if(!content || !optWeights) return;
    
    let totalCurWeight = results.reduce((sum, r) => sum + ((!r.error && r.weight) ? r.weight : 1), 0);
    let html = `<div style="display:grid; grid-template-columns: 80px 1fr 1fr; gap:1rem; font-size:0.85rem; font-weight:600; color:var(--text-muted); margin-bottom:1rem; border-bottom:1px solid var(--card-border); padding-bottom:0.5rem;"><div>Hisse</div><div>Mevcut Ağırlık</div><div>İdeal (% Ağırlık)</div></div>`;

    for (const [ticker, idealPct] of Object.entries(optWeights)) {
        const r = results.find(x => x.ticker === ticker);
        let curPct = 0;
        if (r && !r.error) {
            curPct = ((r.weight || 1) / totalCurWeight) * 100;
        }

        html += `
        <div style="display:grid; grid-template-columns: 80px 1fr 1fr; gap:1rem; align-items:center; margin-bottom:0.75rem;">
            <div style="font-weight:700;">${ticker}</div>
            
            <div style="display:flex; align-items:center; gap:0.5rem;">
                <div style="flex:1; height:8px; background:rgba(255,255,255,0.05); border-radius:4px; overflow:hidden;">
                    <div style="height:100%; width:${curPct}%; background:var(--text-muted); border-radius:4px;"></div>
                </div>
                <span style="width:50px; text-align:right">%${curPct.toFixed(1)}</span>
            </div>
            
            <div style="display:flex; align-items:center; gap:0.5rem;">
                <div style="flex:1; height:8px; background:rgba(255,255,255,0.05); border-radius:4px; overflow:hidden;">
                    <div style="height:100%; width:${idealPct}%; background:var(--primary); box-shadow:0 0 8px rgba(14, 165, 233, 0.4); border-radius:4px;"></div>
                </div>
                <span style="width:50px; text-align:right; color:var(--primary); font-weight:700;">%${idealPct.toFixed(1)}</span>
            </div>
        </div>`;
    }
    content.innerHTML = html;
}

// ═══════════════════════════════════════
// NEW PRO UX CHARTS
// ═══════════════════════════════════════

window.createRadarChart = createRadarChart;
function createRadarChart(canvasId, result) {
    const canvas = document.getElementById(canvasId);
    if (!canvas || !result.radar_score || result.error) return;

    const { profitability, growth, value, debt } = result.radar_score;
    // Radar needs an array of data points
    const dataPoints = [profitability, growth, value, debt];
    
    destroyChart(canvasId);
    
    chartInstances[canvasId] = new Chart(canvas, {
        type: 'radar',
        data: {
            labels: ['Karlılık', 'Büyüme', 'Değerleme', 'Borçluluk (Güç)'],
            datasets: [{
                label: 'Finansal Sağlık Skoru',
                data: dataPoints,
                backgroundColor: 'rgba(14, 165, 233, 0.2)',
                borderColor: 'rgba(14, 165, 233, 1)',
                pointBackgroundColor: 'rgba(14, 165, 233, 1)',
                pointBorderColor: '#fff',
                pointHoverBackgroundColor: '#fff',
                pointHoverBorderColor: 'rgba(14, 165, 233, 1)',
                borderWidth: 2,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                r: {
                    angleLines: { color: getChartColors().grid },
                    grid: { color: getChartColors().grid },
                    pointLabels: {
                        color: getChartColors().text,
                        font: { size: 10, family: 'Inter' }
                    },
                    ticks: {
                        display: false,
                        min: 0,
                        max: 100,
                        stepSize: 25
                    }
                }
            },
            plugins: {
                legend: { display: false }
            }
        }
    });
}

window.createGaugeChart = createGaugeChart;
function createGaugeChart(canvasId, score, labelId, valId) {
    const canvas = document.getElementById(canvasId);
    if (!canvas || score === undefined || score === null) return;
    
    // Determine color and text based on score
    let color = '#38bdf8'; // Neutral blue
    let label = 'Nötr';
    
    if (score >= 80) { color = '#166534'; label = 'Güçlü Al'; }
    else if (score >= 60) { color = '#22c55e'; label = 'Al'; }
    else if (score <= 20) { color = '#991b1b'; label = 'Güçlü Sat'; }
    else if (score <= 40) { color = '#ef4444'; label = 'Sat'; }

    // Update the DOM text if elements exist
    if (valId && document.getElementById(valId)) {
        document.getElementById(valId).textContent = score;
        document.getElementById(valId).style.color = color;
    }
    if (labelId && document.getElementById(labelId)) {
        document.getElementById(labelId).textContent = label;
    }

    destroyChart(canvasId);
    
    chartInstances[canvasId] = new Chart(canvas, {
        type: 'doughnut',
        data: {
            datasets: [{
                data: [score, 100 - score],
                backgroundColor: [color, 'rgba(255,255,255,0.05)'],
                borderWidth: 0,
                circumference: 180,
                rotation: 270,
                cutout: '75%'
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                tooltip: { enabled: false }
            },
            animation: { animateRotate: true, animateScale: false }
        }
    });
}

window.createRelativePerformanceChart = createRelativePerformanceChart;
function createRelativePerformanceChart(canvasId, relPerfData) {
    const canvas = document.getElementById(canvasId);
    if (!canvas || !relPerfData) return;

    const { benchmark, dates, stock_history, bm_history } = relPerfData;
    const { grid, text } = getChartColors();
    
    destroyChart(canvasId);
    
    chartInstances[canvasId] = new Chart(canvas, {
        type: 'line',
        data: {
            labels: dates,
            datasets: [
                {
                    label: 'Hisse',
                    data: stock_history,
                    borderColor: 'rgba(14, 165, 233, 1)',
                    backgroundColor: 'transparent',
                    borderWidth: 2,
                    pointRadius: 0,
                    pointHoverRadius: 4,
                    tension: 0.2
                },
                {
                    label: benchmark,
                    data: bm_history,
                    borderColor: 'rgba(245, 158, 11, 0.8)',
                    backgroundColor: 'transparent',
                    borderDash: [5, 5],
                    borderWidth: 2,
                    pointRadius: 0,
                    pointHoverRadius: 4,
                    tension: 0.2
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {
                mode: 'index',
                intersect: false,
            },
            plugins: {
                legend: {
                    position: 'top',
                    labels: { color: text, font: { size: 10, family: 'Inter' }, boxWidth: 12 }
                },
                tooltip: {
                    callbacks: {
                        label: function(context) {
                            let label = context.dataset.label || '';
                            if (label) { label += ': '; }
                            if (context.parsed.y !== null) {
                                label += context.parsed.y.toFixed(1);
                                if (context.dataIndex === 0) label += ' (Baslangıc)';
                            }
                            return label;
                        }
                    }
                }
            },
            scales: {
                x: {
                    display: false, // hide dates to save space
                },
                y: {
                    grid: { color: grid },
                    ticks: { color: text, font: { size: 10 } }
                }
            }
        }
    });
}

// ═══════════════════════════════════════
// OPTIMIZATION CHART (Phase 6)
// ═══════════════════════════════════════
window.renderOptChart = renderOptChart;
function renderOptChart(id, curWeights, optWeights) {
    const canvas = document.getElementById(id);
    if (!canvas) return;
    
    // global chartInstances clear
    if (typeof destroyChart === "function") {
        destroyChart(id);
    }

    const tickers = Object.keys(optWeights);
    const curData = tickers.map(t => curWeights[t] || 0);
    const optData = tickers.map(t => optWeights[t] || 0);
    
    const { grid, text } = getChartColors();
    
    if (typeof chartInstances === "undefined") window.chartInstances = {};

    chartInstances[id] = new Chart(canvas, {
         type: 'bar',
         data: {
              labels: tickers,
              datasets: [
                   { label: 'Mevcut Dağılım (%)', data: curData, backgroundColor: 'rgba(148, 163, 184, 0.3)', borderColor: 'rgba(148, 163, 184, 0.6)', borderWidth: 1, borderRadius: 4 },
                   { label: 'Optimum Dağılım (%)', data: optData, backgroundColor: 'rgba(14, 165, 233, 0.6)', borderColor: 'rgba(14, 165, 233, 1)', borderWidth: 1, borderRadius: 4 }
              ]
         },
         options: {
              responsive: true,
              maintainAspectRatio: false,
              plugins: { 
                  legend: { position: 'top', labels: { color: text, font: { size: 11, family: 'Inter' }, boxWidth: 12 } },
                  tooltip: {
                      callbacks: {
                          label: function(context) {
                              return `${context.dataset.label}: %${context.parsed.y.toFixed(1)}`;
                          }
                      }
                  }
              },
              scales: {
                   x: { grid: { display: false }, ticks: { color: text, font: { size: 11 } } },
                   y: { grid: { color: grid }, ticks: { color: text, font: { size: 10 }, callback: v => '%' + v } }
              }
          }
     });
}

// ═══════════════════════════════════════
// EQUITY CURVE CHART (Portfolio Snapshots)
// ═══════════════════════════════════════
window.createEquityCurveChart = createEquityCurveChart;
function createEquityCurveChart(containerId, snapData) {
    const container = document.getElementById(containerId);
    if (!container || !snapData || snapData.length === 0) return;

    if (tvChartInstances[containerId]) {
        try { tvChartInstances[containerId].remove(); } catch (e) { /* ignore */ }
        delete tvChartInstances[containerId];
    }
    container.innerHTML = "";

    const isDark = document.documentElement.getAttribute("data-theme") !== "light" || (!localStorage.getItem("theme") && window.matchMedia("(prefers-color-scheme: dark)").matches);

    const chartOptions = {
        layout: {
            background: { type: 'solid', color: 'transparent' },
            textColor: isDark ? '#94a3b8' : '#64748b',
        },
        grid: {
            vertLines: { color: isDark ? 'rgba(148,163,184,0.05)' : 'rgba(0,0,0,0.04)' },
            horzLines: { color: isDark ? 'rgba(148,163,184,0.05)' : 'rgba(0,0,0,0.04)' },
        },
        crosshair: { mode: 1 },
        rightPriceScale: { borderVisible: false },
        timeScale: { borderVisible: false, timeVisible: true },
        handleScroll: true,
        handleScale: true,
    };

    const chart = LightweightCharts.createChart(container, chartOptions);
    tvChartInstances[containerId] = chart;

    const resizeObserver = new ResizeObserver(entries => {
        if (entries[0].contentRect.width > 0) {
            chart.applyOptions({ width: entries[0].contentRect.width, height: container.clientHeight || 250 });
        }
    });
    resizeObserver.observe(container);

    const areaSeries = chart.addAreaSeries({
        lineColor: '#22c55e',
        topColor: 'rgba(34, 197, 94, 0.3)',
        bottomColor: 'rgba(34, 197, 94, 0.0)',
        lineWidth: 3,
        title: 'Özsermaye (Equity)',
    });

    const data = snapData.map(s => {
        const t = Math.floor(new Date(s.timestamp).getTime() / 1000);
        return { time: t, value: parseFloat(s.total_value) };
    }).sort((a, b) => a.time - b.time);

    if (data.length > 0) {
        areaSeries.setData(data);
    }

    chart.timeScale().fitContent();
}
