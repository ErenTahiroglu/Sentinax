const API_BASE = '/buffett';

function unavailable(key, reason) {
    return {
        data_status: 'UNAVAILABLE',
        reason_code: 'API_UNREACHABLE',
        reason,
        [key]: [],
        warnings: [],
        as_of: null,
        generated_at: null
    };
}

async function fetchState(path, key) {
    try {
        const response = await fetch(`${API_BASE}/${path}`);
        if (!response.ok) throw new Error('API Error');
        const data = await response.json();
        if (!data || data.data_status !== 'AVAILABLE') {
            return { ...unavailable(key, 'Veri kullanılamıyor.'), ...data, [key]: [] };
        }
        return data;
    } catch (error) {
        console.error(`Failed to fetch ${path}:`, error);
        return unavailable(key, 'Sunucuya ulaşılamadı; veri gösterilemiyor.');
    }
}

window.api = {
    fetchPortfolio: () => fetchState('portfolio', 'portfolio'),
    fetchHistory: () => fetchState('history', 'changes')
};
