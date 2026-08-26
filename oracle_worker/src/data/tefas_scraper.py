import polars as pl
import logging
import datetime
import gc
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from curl_cffi import requests
from typing import List, Dict

logger = logging.getLogger(__name__)

class TefasScraperPolars:
    def __init__(self):
        self.url_base = "https://www.tefas.gov.tr/FonAnaliz.aspx"
        self.url_api = "https://www.tefas.gov.tr/api/DB/BindHistoryInfo"
        self.session = requests.Session(impersonate="chrome")
        self._session_ready = False

    def _ensure_session(self):
        if not self._session_ready:
            try:
                self.session.get(self.url_base, timeout=15)
                self._session_ready = True
            except Exception as e:
                logger.error(f"Failed to initialize TEFAS session: {e}")

    def _fetch_chunk(self, fonkod: str, start_date: str, end_date: str, fontip: str = "YAT"):
        payload = {
            "fontip": fontip,
            "sfonkod": fonkod,
            "bastarih": start_date,
            "bittarih": end_date
        }
        
        try:
            response = self.session.post(
                self.url_api, 
                data=payload,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": self.url_base
                },
                timeout=(15, 60)
            )
            if response.status_code == 200:
                return response.json().get("data", [])
        except Exception as e:
            logger.error(f"Chunk fetch error for {fonkod}: {e}")
        return []

    def fetch_fund(self, fonkod: str, start_date: datetime.date, end_date: datetime.date) -> pl.DataFrame:
        self._ensure_session()
        
        all_data = []
        # Try YAT then EMK
        for tip in ["YAT", "EMK"]:
            data = self._fetch_chunk(fonkod, start_date.strftime("%d.%m.%Y"), end_date.strftime("%d.%m.%Y"), tip)
            if data:
                all_data = data
                break
        
        if not all_data:
            return pl.DataFrame(schema={"Date": pl.Datetime, "Close": pl.Float64})
            
        # Parse into Polars
        df = pl.from_dicts(all_data)
        
        # Zero-Copy / Lazy Processing approach
        df = (
            df.lazy()
            .with_columns([
                # Handle /Date(ms)/ format
                pl.col("TARIH")
                  .str.extract(r"(\d+)")
                  .cast(pl.Int64)
                  .from_epoch(time_unit="ms")
                  .alias("Date"),
                pl.col("FIYAT").cast(pl.Float64).alias("Close")
            ])
            .select(["Date", "Close"])
            .sort("Date")
            .unique("Date")
            .collect()
        )
        
        gc.collect()
        return df

def fetch_all_tefas_funds(funds: List[str], days_back: int = 730) -> Dict[str, pl.DataFrame]:
    """
    Fetches multiple funds with a strict concurrency limit of 2.
    """
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days_back)
    
    scraper = TefasScraperPolars()
    results = {}
    
    # RULE: Concurrency limited to 2
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_to_fund = {executor.submit(scraper.fetch_fund, fund, start, end): fund for fund in funds}
        for future in as_completed(future_to_fund):
            fund = future_to_fund[future]
            try:
                df = future.result()
                results[fund] = df
                logger.info(f"Fetched {fund}: {len(df)} records")
            except Exception as e:
                logger.error(f"Error fetching {fund}: {e}")
                
    return results
