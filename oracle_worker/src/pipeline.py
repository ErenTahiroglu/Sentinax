import polars as pl
import logging
import gc
from .data.whitelist import get_allowed_assets, get_supabase_client
from .data.tefas_scraper import fetch_all_tefas_funds
from .data.bist_analyzer import BistAnalyzerPolars
from .ml.xgb_trainer import XgbTrainerMemoryOptimized

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def run_ml_pipeline():
    # 1. Get Whitelist
    whitelist = get_allowed_assets()
    if not whitelist:
        logger.warning("No active assets found in whitelist.")
        return
    
    symbols = [a["symbol"] for a in whitelist]
    funds = [a["symbol"] for a in whitelist if a["asset_type"] == "FON"]
    
    # 2. Ingest Data (Limited Concurrency)
    logger.info(f"Ingesting data for {len(funds)} funds...")
    raw_data_map = fetch_all_tefas_funds(funds)
    
    # 3. Analyze & Feature Engineering
    analyzer = BistAnalyzerPolars()
    processed_results = []
    
    for symbol, df in raw_data_map.items():
        if df.is_empty(): continue
        
        analysis = analyzer.analyze_ticker(symbol, df)
        processed_results.append(analysis)
        
        # Free memory per ticker
        del df
        gc.collect()

    if not processed_results:
        logger.warning("No data processed.")
        return

    # 4. ML Training (Example logic)
    # In a real scenario, we'd have a training set. 
    # Here we show the scoring/prediction step.
    
    # 5. Deadlock-Protected Upsert to Supabase
    # RULE: Sort payload by primary key (symbol) to prevent deadlocks
    logger.info("Preparing deterministic upsert payload...")
    
    # Convert list of dicts to Polars to sort easily
    results_df = pl.DataFrame(processed_results)
    
    # Sort by symbol (Primary Key)
    sorted_df = results_df.sort("ticker")
    
    # Convert back to list of dicts for Supabase client
    payload = sorted_df.to_dicts()
    
    # Perform Upsert
    supabase = get_supabase_client()
    try:
        # Assuming we have an 'analysis_results' table
        # .upsert() handles ON CONFLICT in PostgreSQL
        res = supabase.table("analysis_results").upsert(payload).execute()
        logger.info(f"Successfully upserted {len(payload)} records to Supabase.")
    except Exception as e:
        logger.error(f"Supabase upsert failed: {e}")
    
    # Final cleanup
    del processed_results
    del sorted_df
    gc.collect()

if __name__ == "__main__":
    run_ml_pipeline()
