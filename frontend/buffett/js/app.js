document.addEventListener('DOMContentLoaded', async () => {
    // Tab Navigation
    const btnDashboard = document.getElementById('btn-dashboard');
    const btnHistory = document.getElementById('btn-history');
    
    const viewDashboard = document.getElementById('view-dashboard');
    const viewHistory = document.getElementById('view-history');
    
    btnDashboard.addEventListener('click', () => {
        btnDashboard.classList.add('active');
        btnHistory.classList.remove('active');
        viewDashboard.classList.add('active');
        viewHistory.classList.remove('active');
    });
    
    btnHistory.addEventListener('click', async () => {
        btnHistory.classList.add('active');
        btnDashboard.classList.remove('active');
        viewHistory.classList.add('active');
        viewDashboard.classList.remove('active');
        
        // Load history when tab is clicked
        const history = await window.api.fetchHistory();
        window.ui.renderHistoryList(history);
    });
    
    // Initial Load - Dashboard
    const portfolio = await window.api.fetchPortfolio();
    window.ui.renderPortfolioGrid(portfolio);
});
