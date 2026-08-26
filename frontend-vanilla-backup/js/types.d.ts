/**
 * 🟡 TypeScript Tip Tanımları — P4 Öncelik Matrisi (Adım 1)
 * ===========================================================
 * Tüm proje boyunca kullanılan ortak veri yapıları.
 * Bu dosya saf tip bildirimidir — hiçbir runtime kodu içermez.
 *
 * Kullanım (JSDoc ile JS dosyalarında):
 *   @param {AnalysisResult} result
 *   @type {AppStateType}
 */

// ── Analiz Sonucu ─────────────────────────────────────────────────────────

/** @typedef {Object} PriceInfo
 * @property {number} fiyat - Güncel fiyat
 * @property {number} degisim - Günlük değişim %
 */

/** @typedef {Object} FinancialData
 * @property {PriceInfo} [son_fiyat]
 * @property {number} [s5] - 5 yıllık CAGR %
 * @property {number} [s1] - 1 yıllık getiri %
 * @property {number} [sharpe] - Sharpe oranı
 * @property {number} [max_drawdown] - Maksimum düşüş %
 */

/** @typedef {Object} ValuationData
 * @property {number} [pe] - F/K oranı
 * @property {number} [pb] - F/DD oranı
 * @property {number} [beta] - Beta katsayısı
 * @property {number} [market_cap] - Piyasa değeri (USD)
 * @property {number} [dividend_yield] - Temettü verimi %
 */

/** @typedef {Object} TechnicalData
 * @property {number} [rsi] - RSI (14)
 * @property {Object} [macd] - MACD sinyal verileri
 * @property {number} [gauge_score] - Teknik kadran skoru (0-100)
 * @property {string} [trend] - "YUKARI" | "ASAGI" | "YATAY"
 */

/** @typedef {Object} MLPrediction
 * @property {"UP"|"DOWN"|"SIDEWAYS"} direction - Yön tahmini
 * @property {number} confidence - Güven skoru (0-1)
 * @property {number} target_7d - 7 günlük fiyat hedefi
 * @property {Array<{ds: string, yhat: number, yhat_lower: number, yhat_upper: number}>} [plot_data]
 */

/** @typedef {Object} SentimentData
 * @property {string} label - "OLUMLU" | "OLUMSUZ" | "NÖTR"
 * @property {number} score - Duygu skoru (-1 ile 1 arası)
 * @property {string} [summary]
 */

/** @typedef {Object} IslamicData
 * @property {boolean} is_compliant
 * @property {number} [purification_ratio]
 * @property {number} [debt_ratio]
 * @property {string} [verdict]
 */

/** @typedef {Object} OptionsData
 * @property {string} expiration
 * @property {Array<OptionContract>} calls
 * @property {Array<OptionContract>} puts
 */

/** @typedef {Object} OptionContract
 * @property {number} strike - Kullanım fiyatı
 * @property {number} [lastPrice] - Son işlem fiyatı
 * @property {number} [bid] - Alış
 * @property {number} [ask] - Satış
 * @property {number} [volume] - Hacim
 * @property {number} [openInterest] - Açık faiz
 * @property {number} [impliedVolatility] - İçsel oynaklık
 * @property {boolean} [inTheMoney] - Para içi mi?
 */

/** @typedef {Object} AnalysisResult
 * @property {string} ticker - Hisse sembolü (ör: "AAPL")
 * @property {string} market - "US" | "BIST" | "TEFAS" | "CRYPTO"
 * @property {string} [status] - Analiz durumu
 * @property {FinancialData} [financials]
 * @property {ValuationData} [valuation]
 * @property {TechnicalData} [technicals]
 * @property {SentimentData} [sentiment]
 * @property {MLPrediction} [ml_prediction]
 * @property {IslamicData} [islamic]
 * @property {OptionsData} [options]
 * @property {number} [weight] - Portföy ağırlığı %
 * @property {string} [error] - Hata mesajı
 * @property {string} [fin_error] - Finansal veri hata mesajı
 */

// ── Uygulama State ────────────────────────────────────────────────────────

/** @typedef {Object} AppStateType
 * @property {AnalysisResult[]} results - Analiz sonuçları dizisi
 * @property {Record<string, unknown>|null} extras - İstemci tarafı hesaplamalar
 * @property {boolean} isLoading
 * @property {string|null} activeTab
 */

// ── API İstek/Yanıt Tipleri ───────────────────────────────────────────────

/** @typedef {Object} AnalyzeRequest
 * @property {string[]} tickers
 * @property {boolean} [check_islamic]
 * @property {boolean} [check_financials]
 * @property {boolean} [use_ai]
 * @property {string} [api_key]
 * @property {string} [model]
 * @property {string} [lang]
 */

/** @typedef {Object} LivePriceMessage
 * @property {"T"|"Q"} ev - Event tipi (Trade/Quote)
 * @property {string} sym - Ticker sembolü
 * @property {number} p - Fiyat
 * @property {number} s - İşlem miktarı
 * @property {number} t - Timestamp (ms)
 */

export {};  // Bu dosyayı bir ES modülü olarak işaretler
