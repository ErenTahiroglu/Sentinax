import numpy as np
import pandas as pd
import logging
import gc

logger = logging.getLogger(__name__)

def optimize_portfolio(returns_df: pd.DataFrame, risk_free_rate: float = 0.0) -> dict:
    """
    Kovaryans matrisi üzerinden 3 farklı portföy optimizasyonu hesaplar:
    1. Maksimum Sharpe Oranı
    2. Minimum Varyans (Risk)
    3. Maksimum Getiri
    """
    tickers = list(returns_df.columns)
    num_assets = len(tickers)
    
    def get_default_w():
        return {t: round(100.0 / max(num_assets, 1), 2) for t in tickers}
    
    # 1. Veri Doğrulama (Sanitization)
    if returns_df.empty:
        logger.warning("optimize_portfolio: Gelen returns_df tamamen boş! Fallback uygulanıyor.")
        default_w = get_default_w()
        return {"max_sharpe": default_w, "min_volatility": default_w, "max_return": default_w}
        
    returns_df = returns_df.replace([np.inf, -np.inf], np.nan).dropna()
    
    if len(returns_df) < 2 or num_assets < 2:
        logger.warning(f"optimize_portfolio: Yetersiz veri (Satır: {len(returns_df)}, Varlık: {num_assets}). Fallback uygulanıyor.")
        default_w = get_default_w()
        return {"max_sharpe": default_w, "min_volatility": default_w, "max_return": default_w}
    
    # Başlangıç durumu için eşit dağılım kopyaları oluşturuluyor
    default_w = get_default_w()
    results = {
        "max_sharpe": default_w.copy(), 
        "min_volatility": default_w.copy(), 
        "max_return": default_w.copy()
    }
    
    # 2. Matematiksel Hata Yakalama Bloğu
    try:
        mean_returns = returns_df.mean() * 252
        cov_matrix = returns_df.cov() * 252
        
        # 3. Monte Carlo Simülasyonu (Numpy Only)
        num_iterations = 5000
        
        # Önceden rastgele ağırlık matrisini üret (Hızlı çarpım için)
        weights_matrix = np.random.random((num_iterations, num_assets))
        # Satırları toplayıp normalize et (Ağırlıklar toplamı = 1.0 olsun)
        weights_matrix = weights_matrix / weights_matrix.sum(axis=1)[:, np.newaxis]
        
        # Portföy Getirileri
        port_returns = np.dot(weights_matrix, mean_returns)
        
        # Portföy Volatiliteleri (Vektörize edilmiş kovaryans çarpımı)
        port_vols = np.zeros(num_iterations)
        for i in range(num_iterations):
            w = weights_matrix[i]
            port_vols[i] = np.sqrt(np.dot(w.T, np.dot(cov_matrix, w)))
            
        # Sharpe Oranları
        sharpe_ratios = np.zeros(num_iterations)
        # 0'a bölme riskini engelle
        valid_vols = port_vols > 0
        sharpe_ratios[valid_vols] = (port_returns[valid_vols] - risk_free_rate) / port_vols[valid_vols]
        
        # İndeksleri Bul
        max_sharpe_idx = np.argmax(sharpe_ratios)
        min_vol_idx = np.argmin(port_vols)
        # Max Getiri (Sharpe değil, sadece return)
        max_ret_idx = np.argmax(port_returns)
        
        def format_weights(w_array):
            w_dict = {}
            for i, t in enumerate(tickers):
                w_dict[t] = round(float(w_array[i]) * 100, 2)
            return w_dict

        results["max_sharpe"] = format_weights(weights_matrix[max_sharpe_idx])
        results["min_volatility"] = format_weights(weights_matrix[min_vol_idx])
        results["max_return"] = format_weights(weights_matrix[max_ret_idx])
        
        # 4. Bellek Temizliği (OOM Önlemi)
        del weights_matrix
        del port_returns
        del port_vols
        del sharpe_ratios
            
    except Exception as e:
        logger.warning(f"optimize_portfolio sırasında kritik hata oluştu (Numpy): {e}")
        # Hata durumunda results, fonksiyonun başında eşit ağırlıkla tanımlanmış halleriyle kalır.
    finally:
        # Zorunlu Garbage Collection
        gc.collect()
        
    return results
