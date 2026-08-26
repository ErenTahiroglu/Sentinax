// ═══════════════════════════════════════
// ANALYSIS & BACKTEST MODULE
// ═══════════════════════════════════════

export function setupBacktestBindings() {
    const initBalanceInput = document.getElementById("sim-initial-balance");
    const monthlyContInput = document.getElementById("sim-monthly-contribution");
    const rebalanceInput = document.getElementById("sim-rebalance-freq");

    if (!initBalanceInput || !monthlyContInput || !rebalanceInput) return;

    function triggerRecalculation() {
        const payload = {
            initial_balance: parseFloat(initBalanceInput.value) || 10000,
            monthly_contribution: parseFloat(monthlyContInput.value) || 0,
            rebalancing_freq: rebalanceInput.value
        };

        const results = (typeof AppState !== "undefined" && AppState.results) || window.lastResults || [];
        if (results.length === 0) return;

        if (typeof runPVSimulationJS === "function") {
            const validResults = results.filter(r => !r.error && r.technicals?.relative_performance);
            if (validResults.length === 0) return;

            const simRes = runPVSimulationJS(validResults, payload);
            if (simRes) {
                if (typeof AppState !== "undefined") {
                    AppState.extras = { ...(AppState.extras || {}), pv_simulation: simRes };
                }
                try {
                    if (typeof createBacktestChart === "function") createBacktestChart("bt-chart-container", simRes);
                } catch (e) { /* ignore */ }
            }
        }
    }

    initBalanceInput.addEventListener("input", triggerRecalculation);
    monthlyContInput.addEventListener("input", triggerRecalculation);
    rebalanceInput.addEventListener("change", triggerRecalculation);
}

export async function fetchAndRenderSignals(tickers) {
    const container = document.getElementById("signal-items-container");
    const widget = document.getElementById("radar-signal-widget");
    if (!container || !widget || !tickers) return;

    try {
        const response = await fetch(`${window.API_BASE}/api/portfolio-signals?tickers=${encodeURIComponent(tickers)}`);
        const data = await response.json();
        
        if (data.length === 0) {
            widget.classList.add("hidden");
            return;
        }

        widget.classList.remove("hidden");
        container.innerHTML = ""; 

        data.forEach(item => {
            if (item.signals && item.signals.length > 0) {
                item.signals.forEach(s => {
                    const isBull = s.signal === "BULLISH";
                    const color = isBull ? "var(--success)" : "var(--danger)";
                    const icon = isBull ? "fa-arrow-circle-up" : "fa-arrow-circle-down";
                    
                    const el = document.createElement("div");
                    el.style.display = "inline-flex";
                    el.style.alignItems = "center";
                    el.style.gap = "5px";
                    el.style.padding = "0.35rem 0.65rem";
                    el.style.background = isBull ? "rgba(34, 197, 94, 0.08)" : "rgba(239, 68, 68, 0.08)";
                    el.style.border = `1px solid ${isBull ? 'rgba(34,197,94,0.15)' : 'rgba(239,68,68,0.15)'}`;
                    el.style.borderRadius = "6px";
                    el.style.color = color;
                    el.style.fontWeight = "600";
                    el.style.fontSize = "0.82rem";
                    el.style.cursor = "pointer";
                    
                    el.innerHTML = `<i class="fas ${icon}"></i> <span>${item.ticker}: ${s.reason}</span>`;
                    
                    el.addEventListener("click", () => {
                        const input = document.getElementById("ticker-input");
                        if (input) {
                            input.value = item.ticker;
                            const btn = document.getElementById("analyze-btn") || document.getElementById("btn-run");
                            if (btn) btn.click();
                        }
                    });

                    container.appendChild(el);
                });
            }
        });
    } catch (e) {
        console.warn("Signal fetch failed:", e);
    }
}

export function setupOptimization() {
    const optimizeBtn = document.getElementById("btn-optimize-portfolio");
    const content = document.getElementById("optimization-content");
    const aiText = document.getElementById("opt-ai-text");
    
    if (!optimizeBtn) return;
    
    optimizeBtn.addEventListener("click", async () => {
         const currentResults = (typeof AppState !== "undefined" && AppState.results) || window.lastResults || [];
         if (currentResults.length === 0) {
              if (typeof showToast === "function") showToast("Önce bir analiz çalıştırın veya portföy ekleyin.", "warning");
              return;
         }

         const tickers = currentResults.map(r => r.ticker);
         const totalWeight = currentResults.reduce((sum, r) => sum + (r.weight || 1), 0);
         const weights = {};
         currentResults.forEach(r => {
              weights[r.ticker] = ((r.weight || 1) / totalWeight) * 100;
         });

         const origText = optimizeBtn.innerHTML;
         optimizeBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Optimize ediliyor...';
         optimizeBtn.disabled = true;
         
         try {
              const session = await window.SupabaseAuth.getValidSession();
              if (!session) {
                  if (typeof showToast === "function") showToast("Lütfen giriş yapın.", "warning");
                  return;
              }

              const res = await fetch(`${window.API_BASE}/api/optimize-portfolio`, {
                   method: "POST",
                   headers: {
                        "Authorization": `Bearer ${session.access_token}`,
                        "Content-Type": "application/json"
                   },
                   body: JSON.stringify({ tickers, weights })
              });
              
              if (!res.ok) throw new Error("Ağ hatası veya yetersiz veri");
              const data = await res.json();
              
              content.classList.remove("hidden");
              
              if (typeof renderOptChart === "function") {
                  renderOptChart("opt-comparison-chart", data.current_weights, data.optimal_weights);
              }

              const prompt = `Mevcut Varlık Dağılımım: ${JSON.stringify(data.current_weights)}
Maksimum Sharpe Oranına göre Matematiksel Optimum Dağılım: ${JSON.stringify(data.optimal_weights)}

Lütfen bu iki dağılımı karşılaştır. Hangilerini satıp hangilerini almam gerektiğini matematiksel aksiyon adımlarıyla Rebalance (Yeniden Dengeleme) tavsiyesi olarak Türkçe açıkla.`;
              
              if (aiText) aiText.innerHTML = '<i class="fas fa-spinner fa-spin"></i> AI tavsiyesi oluşturuluyor...';
              
              const aiRes = await fetch(`${window.API_BASE}/api/chat`, {
                   method: "POST",
                   headers: { 
                       "Authorization": `Bearer ${session.access_token}`,
                       "Content-Type": "application/json" 
                   },
                   body: JSON.stringify({
                        messages: [{ role: "user", content: prompt }],
                        portfolio_context: currentResults.map(r => ({ ticker: r.ticker }))
                   })
              });
              
              if (aiRes.ok && aiText) {
                   const aiData = await aiRes.json();
                   aiText.innerHTML = aiData.reply || aiData.response || "Yorum oluşturuldu."; 
              } else if (aiText) {
                   aiText.innerText = "Matematiksel optimum bulundu. Detaylar için Copilot'a sorabilirsiniz.";
              }
              
         } catch (e) {
              console.error(e);
              if (typeof showToast === "function") showToast("Optimizasyon başarısız oldu: " + e.message, "danger");
         } finally {
              optimizeBtn.innerHTML = origText;
              optimizeBtn.disabled = false;
         }
    });
}

export function setupRiskAnalysis() {
    const riskBtn = document.getElementById("btn-run-risk-analysis");
    const content = document.getElementById("risk-content");
    const varVal = document.getElementById("risk-var-val");
    const maxddVal = document.getElementById("risk-maxdd-val");
    const betaVal = document.getElementById("risk-beta-val");
    const stressVal = document.getElementById("risk-stress-val");
    const aiText = document.getElementById("risk-ai-text");
    const aiBox = document.getElementById("risk-ai-suggestion");
    const barVar = document.getElementById("bar-var");
    const barMaxdd = document.getElementById("bar-maxdd");
    
    if (!riskBtn) return;
    
    riskBtn.addEventListener("click", async () => {
         const currentResults = (typeof AppState !== "undefined" && AppState.results) || window.lastResults || [];
         if (currentResults.length === 0) {
              if (typeof showToast === "function") showToast("Önce bir analiz çalıştırın veya portföy ekleyin.", "warning");
              return;
         }

         const tickers = currentResults.map(r => r.ticker);
         const totalWeight = currentResults.reduce((sum, r) => sum + (r.weight || 1), 0);
         const weights = {};
         currentResults.forEach(r => {
              weights[r.ticker] = ((r.weight || 1) / totalWeight) * 100;
         });

         const origText = riskBtn.innerHTML;
         riskBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Hesaplanıyor...';
         riskBtn.disabled = true;
         
         try {
              const session = await window.SupabaseAuth.getValidSession();
              if (!session) {
                  if (typeof showToast === "function") showToast("Lütfen giriş yapın.", "warning");
                  return;
              }

              const res = await fetch(`${window.API_BASE}/api/risk-analysis`, {
                   method: "POST",
                   headers: {
                        "Authorization": `Bearer ${session.access_token}`,
                        "Content-Type": "application/json"
                   },
                   body: JSON.stringify({ tickers, weights })
              });
              
              if (!res.ok) throw new Error("Ağ hatası");
              const rx = await res.json();
              
              if (content) content.classList.remove("hidden");
              if (varVal) varVal.innerText = `%${rx.var_95}`;
              if (maxddVal) maxddVal.innerText = `%${rx.max_drawdown}`;
              if (betaVal) betaVal.innerText = rx.weighted_beta;
              if (stressVal) stressVal.innerText = `%${rx.stress_test_shock_drop}`;

              if (barVar) {
                  const absVar = Math.abs(rx.var_95);
                  barVar.style.width = `${Math.min(absVar * 10, 100)}%`;
                  barVar.style.backgroundColor = absVar > 4 ? '#ef4444' : absVar > 2 ? '#f59e0b' : '#22c55e';
              }

              if (barMaxdd) {
                  const absMaxdd = Math.abs(rx.max_drawdown);
                  barMaxdd.style.width = `${Math.min(absMaxdd, 100)}%`;
                  barMaxdd.style.backgroundColor = absMaxdd > 25 ? '#ef4444' : absMaxdd > 15 ? '#f59e0b' : '#22c55e';
              }

              if (aiBox) aiBox.style.display = "block";
              if (aiText) aiText.innerHTML = '<i class="fas fa-spinner fa-spin"></i> AI Müfettişi analiz ediyor...';
              
              const prompt = `Aşağıdaki Portföy Risk İncelemesini değerlendir:\n- Günlük VaR (%95 Güven): %${rx.var_95}\n- Tarihsel Maksimum Düşüş (MaxDD): %${rx.max_drawdown}\n- Portföy Betası: ${rx.weighted_beta}\n- -%20 Piyasa Şoku Efektifi: %${rx.stress_test_shock_drop}`;

              const aiRes = await fetch(`${window.API_BASE}/api/chat`, {
                   method: "POST",
                   headers: { "Authorization": `Bearer ${session.access_token}`, "Content-Type": "application/json" },
                   body: JSON.stringify({ messages: [{ role: "user", content: prompt }] })
              });
              
              if (aiRes.ok && aiText) {
                   const aiData = await aiRes.json();
                   aiText.innerHTML = aiData.reply || aiData.response || "Yorum oluşturuldu."; 
                   if (window.refreshPaperTrades) window.refreshPaperTrades();
              }
         } catch (e) {
              if (typeof showToast === "function") showToast("Risk analizi başarısız oldu: " + e.message, "danger");
         } finally {
              riskBtn.innerHTML = origText;
              riskBtn.disabled = false;
         }
    });
}

export function setupPaperTrades() {
    const tbody = document.getElementById("paper-trades-tbody");
    const refreshBtn = document.getElementById("btn-refresh-trades");
    if (!tbody) return;
    
    async function loadTrades() {
         try {
              const session = await window.SupabaseAuth.getValidSession();
              if (!session) return;

              const res = await fetch(`${window.API_BASE}/api/paper-trades`, {
                   headers: { "Authorization": `Bearer ${session.access_token}` }
              });
              if (!res.ok) return;
              const trades = await res.json();
              
              if (trades.length === 0) {
                   tbody.innerHTML = '<tr><td colspan="4" style="text-align:center; padding:1.5rem; color:var(--text-muted);">Henüz bir işlem kaydı yok.</td></tr>';
                   return;
              }

              tbody.innerHTML = trades.map(t => {
                   const date = new Date(t.timestamp).toLocaleDateString("tr-TR", { hour: '2-digit', minute: '2-digit' });
                   return `<tr><td>${date}</td><td><strong>${t.symbol}</strong></td><td class="${t.order_type === 'BUY' ? 'positive' : 'negative'}">${t.order_type === 'BUY' ? 'ALIM' : 'SATIŞ'}</td><td>%${t.target_weight}</td></tr>`;
              }).join("");
         } catch (e) { /* ignore */ }
    }
    if (refreshBtn) refreshBtn.addEventListener("click", loadTrades);
    setTimeout(loadTrades, 1500);
    window.refreshPaperTrades = loadTrades;
}
