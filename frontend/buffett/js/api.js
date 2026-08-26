const API_BASE = '/buffett';

async function fetchPortfolio() {
    try {
        const response = await fetch(`${API_BASE}/portfolio`);
        if (!response.ok) throw new Error('API Error');
        const data = await response.json();
        return data.portfolio;
    } catch (error) {
        console.error('Failed to fetch portfolio:', error);
        return [];
    }
}

async function fetchHistory() {
    try {
        const response = await fetch(`${API_BASE}/history`);
        if (!response.ok) throw new Error('API Error');
        const data = await response.json();
        return data.changes;
    } catch (error) {
        console.error('Failed to fetch history:', error);
        return [];
    }
}

window.api = {
    fetchPortfolio,
    fetchHistory
};
