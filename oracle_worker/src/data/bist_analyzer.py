import polars as pl
import duckdb
import logging
import gc
from ..config.db_setup import get_duckdb_connection

logger = logging.getLogger(__name__)

class BistAnalyzerPolars:
    def __init__(self, conn=None):
        self.conn = conn or get_duckdb_connection()

    def process_features(self, df: pl.DataFrame) -> pl.DataFrame:
        """
        Processes financial features using DuckDB and Polars (Zero-Copy).
        """
        if df.is_empty():
            return df

        # Register Polars DataFrame as a virtual table in DuckDB
        self.conn.register("raw_data", df)
        
        # RULE: Use DuckDB for aggregations and window functions (efficient)
        # We compute Moving Averages, RSI, and Volatility
        query = """
        SELECT 
            Date, 
            Close,
            AVG(Close) OVER (ORDER BY Date ROWS BETWEEN 20 PRECEDING AND CURRENT ROW) as SMA_20,
            AVG(Close) OVER (ORDER BY Date ROWS BETWEEN 50 PRECEDING AND CURRENT ROW) as SMA_50,
            STDDEV(Close) OVER (ORDER BY Date ROWS BETWEEN 20 PRECEDING AND CURRENT ROW) as Volatility_20,
            (Close - LAG(Close, 1) OVER (ORDER BY Date)) / LAG(Close, 1) OVER (ORDER BY Date) as Daily_Return
        FROM raw_data
        ORDER BY Date
        """
        
        # Execute query and return as Polars DataFrame (Zero-Copy)
        processed_df = self.conn.execute(query).pl()
        
        # Unregister to free resources
        self.conn.unregister("raw_data")
        
        # Polars-side feature engineering (Lazy)
        processed_df = (
            processed_df.lazy()
            .with_columns([
                (pl.col("Close") / pl.col("SMA_20")).alias("Price_to_SMA20"),
                (pl.col("Daily_Return").rolling_mean(window_size=14)).alias("Avg_Return_14d")
            ])
            .drop_nulls()
            .collect()
        )
        
        gc.collect()
        return processed_df

    def analyze_ticker(self, ticker: str, df: pl.DataFrame) -> Dict:
        """
        Analyzes a single ticker and returns a summary.
        """
        features = self.process_features(df)
        if features.is_empty():
            return {"ticker": ticker, "status": "no_data"}
            
        latest = features.tail(1).to_dicts()[0]
        
        return {
            "ticker": ticker,
            "last_close": latest["Close"],
            "sma_20": latest["SMA_20"],
            "volatility": latest["Volatility_20"],
            "daily_return": latest["Daily_Return"]
        }
