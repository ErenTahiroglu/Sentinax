import { showToast } from '../utils.js';
import { appState } from '../core/appState.js';

export async function runFileAnalysis(file, onComplete) {
    if (!file) return;
    showToast(appState.lang === "en" ? "Parsing file..." : "Dosya okunuyor...", "info");

    const reader = new FileReader();
    const extension = file.name.split('.').pop().toLowerCase();

    reader.onload = function(e) {
        let tickers = [];
        try {
            if (extension === 'xlsx' || extension === 'xls') {
                const data = new Uint8Array(e.target.result);
                const workbook = XLSX.read(data, { type: 'array' });
                const csv = XLSX.utils.sheet_to_csv(workbook.Sheets[workbook.SheetNames[0]]);
                tickers = extractTickersFromCsv(csv);
            } else {
                tickers = extractTickersFromCsv(e.target.result);
            }

            if (tickers.length === 0) return showToast("Geçerli sembol bulunamadı.", "warning");
            onComplete(tickers);

        } catch (err) {
            showToast(`Dosya hatası: ${err.message}`, "error");
        }
    };

    if (extension === 'xlsx' || extension === 'xls') reader.readAsArrayBuffer(file);
    else reader.readAsText(file);
}

function extractTickersFromCsv(text) {
    const matches = text.match(/[A-Z]+(\.[A-Z]+)?(:[0-9.]+)?/g);
    return matches ? [...new Set(matches.filter(m => m.length >= 2))] : [];
}
