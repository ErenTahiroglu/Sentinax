// ═══════════════════════════════════════
// EXPORT SERVICE
// ═══════════════════════════════════════

export async function exportResults(format, results) {
    if (!results || results.length === 0) { 
        showToast(t("toast.noTickers") || "Dışa aktarılacak veri bulunamadı", "warning"); 
        return; 
    }
    
    showToast(`${format.toUpperCase()} ${t("toast.exporting") || "Dışa aktarılıyor..."}`, "info");

    // Client-side Excel Export using SheetJS (XLSX)
    if (format === 'excel' && typeof XLSX !== 'undefined') {
        try {
            const rows = results.map(res => {
                const fin = res.financials || {};
                const val = res.valuation || {};
                return {
                    "Hisse/Fon": res.ticker || "",
                    "Pazar": res.market || "",
                    "Durum": res.status || "-",
                    "Arındırma Oranı (%)": res.purification_ratio !== undefined ? res.purification_ratio : "-",
                    "Borçluluk Oranı (%)": res.debt_ratio !== undefined ? res.debt_ratio : "-",
                    "5Y Reel Getiri (%)": fin.s5 !== undefined ? fin.s5 : "-",
                    "3Y Reel Getiri (%)": fin.s3 !== undefined ? fin.s3 : "-",
                    "P/E": val.pe !== undefined ? val.pe : "-",
                    "P/B": val.pb !== undefined ? val.pb : "-",
                    "Beta": val.beta !== undefined ? val.beta : "-"
                };
            });

            const ws = XLSX.utils.json_to_sheet(rows);
            const wb = XLSX.utils.book_new();
            XLSX.utils.book_append_sheet(wb, ws, "Portföy Analizi");
            XLSX.writeFile(wb, "portfoy_analizi.xlsx");
            showToast(t("toast.exported") || "Başarıyla dışa aktarıldı!", "success");
            return;
        } catch (err) {
            console.error("XLSX Export Error:", err);
            showToast(`Excel aktarma hatası: ${err.message}`, "error");
            return;
        }
    }

    try {
        const res = await fetch(`${API_BASE}/api/export/${format}`, { 
            method: "POST", 
            headers: { "Content-Type": "application/json" }, 
            body: JSON.stringify({ results, format }) 
        });
        
        if (!res.ok) throw new Error("Export failed");
        
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `portfoy_analizi.${format === "excel" ? "xlsx" : format}`;
        document.body.appendChild(a); 
        a.click(); 
        a.remove();
        URL.revokeObjectURL(url);
        showToast(t("toast.exported") || "Başarıyla dışa aktarıldı!", "success");
    } catch (err) { 
        showToast(`Export hatası: ${err.message}`, "error"); 
    }
}

export async function exportPortfolioImage(results) {
    const resultsElem = document.getElementById("results");
    if (!results || results.length === 0) { 
        showToast(t("toast.noTickers") || "Dışa aktarılacak veri bulunamadı", "warning"); 
        return; 
    }

    showToast("Görsel hazırlanıyor...", "info");
    try {
        if (typeof html2canvas === 'undefined') {
            throw new Error("html2canvas kütüphanesi yüklenemedi.");
        }

        const canvas = await html2canvas(resultsElem, {
            scale: 2,
            backgroundColor: getComputedStyle(document.documentElement).getPropertyValue('--bg-body') || '#0f172a',
            ignoreElements: (el) => el.classList.contains('toolbar-actions') // Hide buttons in screenshot
        });

        const url = canvas.toDataURL("image/png");
        const a = document.createElement("a");
        a.href = url;
        a.download = `portfoy_analizi.png`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        showToast("Görsel indirildi!", "success");
    } catch (err) {
        console.error(err);
        showToast(`Görsel oluşturma hatası: ${err.message}`, "error");
    }
}
