import duckdb
import os
import logging

logger = logging.getLogger(__name__)

def get_duckdb_connection(db_path=":memory:"):
    """
    Returns a DuckDB connection with strict memory and spill settings
    for Oracle Cloud A1 (ARM64) environment.
    """
    conn = duckdb.connect(db_path)
    
    # Strict memory limits for Zero-Copy architecture
    conn.execute("SET memory_limit='4GB'")
    
    # Ensure spill directory exists for larger-than-RAM joins
    spill_dir = "/tmp/duckdb_spill"
    os.makedirs(spill_dir, exist_ok=True)
    conn.execute(f"SET temp_directory='{spill_dir}'")
    
    logger.info(f"DuckDB initialized with 4GB limit and spill at {spill_dir}")
    return conn
