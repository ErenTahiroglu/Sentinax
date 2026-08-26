import polars as pl
import logging
import gc
from concurrent.futures import ThreadPoolExecutor, as_completed
from curl_cffi import requests
from bs4 import BeautifulSoup
from typing import List, Dict

logger = logging.getLogger(__name__)

class NewsFetcherPolars:
    def __init__(self):
        self.session = requests.Session(impersonate="chrome")

    def fetch_news_for_ticker(self, ticker: str) -> List[Dict]:
        """
        Fetches news from Yahoo Finance using streaming-like parsing
        to keep memory footprint low.
        """
        # Using a search/rss-like URL for easier parsing if available, 
        # or standard finance page.
        url = f"https://finance.yahoo.com/quote/{ticker}/news"
        
        try:
            # RULE: Streaming parse / Low memory
            # We fetch and immediately parse the soup, then extract only what we need.
            response = self.session.get(url, timeout=20)
            if response.status_code != 200:
                return []
            
            soup = BeautifulSoup(response.content, "html.parser")
            # Clear response content from memory
            del response
            
            news_items = []
            # Yahoo specific selectors (may change, usually <h3> or <a> within specific lists)
            articles = soup.find_all("li", class_="js-stream-content")[:10]
            
            for article in articles:
                title_tag = article.find("h3")
                link_tag = article.find("a")
                if title_tag and link_tag:
                    news_items.append({
                        "ticker": ticker,
                        "title": title_tag.get_text().strip(),
                        "link": "https://finance.yahoo.com" + link_tag.get("href", ""),
                        "provider": article.find("span").get_text() if article.find("span") else "Yahoo Finance"
                    })
            
            # Explicitly clear soup
            soup.decompose()
            gc.collect()
            return news_items
            
        except Exception as e:
            logger.error(f"Error fetching news for {ticker}: {e}")
            return []

def fetch_all_news(tickers: List[str]) -> pl.DataFrame:
    """
    Fetches news for multiple tickers with a concurrency limit of 2.
    """
    fetcher = NewsFetcherPolars()
    all_news = []
    
    # RULE: Concurrency limited to 2
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_to_ticker = {executor.submit(fetcher.fetch_news_for_ticker, t): t for t in tickers}
        for future in as_completed(future_to_ticker):
            res = future.result()
            if res:
                all_news.extend(res)
    
    if not all_news:
        return pl.DataFrame(schema={"ticker": pl.String, "title": pl.String, "link": pl.String, "provider": pl.String})
        
    df = pl.from_dicts(all_news)
    gc.collect()
    return df
