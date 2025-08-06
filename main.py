from mcp.server.fastmcp import FastMCP
import requests
from bs4 import BeautifulSoup
import logging
from typing import List, Dict
import urllib.parse  # для экранирования поискового запроса и парсинга URL
import msp_search
import msp_date_time

# ---------- Настройка логгера ----------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger(__name__)

# --- Add file handler for logs after basicConfig
file_handler = logging.FileHandler('msp_server.log')
file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
log.addHandler(file_handler)

# ---------- Сам сервис ----------
mcp = FastMCP("URL Text Fetcher")

@mcp.tool()
def fetch_url_text(url: str) -> str:
    """Download the text from a URL."""
    log.info(f"fetch_url_text() started for {url}")
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
    except Exception as exc:
        log.exception(f"HTTP error while fetching {url}: {exc}")
        raise
    soup = BeautifulSoup(resp.text, "html.parser")
    text = soup.get_text(separator="\n", strip=True)
    log.info(f"fetch_url_text() finished for {url} (size={len(text)} chars)")
    return text

@mcp.tool()
def fetch_page_links(url: str) -> List[str]:
    """Return a list of all URLs found on the given page."""
    log.info(f"fetch_page_links() started for {url}")
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
    except Exception as exc:
        log.exception(f"HTTP error while fetching {url}: {exc}")
        raise
    soup = BeautifulSoup(resp.text, "html.parser")
    links = [str(a["href"]) for a in soup.find_all("a", href=True)]
    log.info(
        f"fetch_page_links() finished for {url} (found {len(links)} links)"
    )
    return links

#TODO
# @mcp.tool()
# def search_google(query: str, num: int = 5) -> List[Dict[str, str]]:
#     driver = msp_search.get_driver()
#     return msp_search.search_google(driver, query, num)

@mcp.tool()
def search_duckduckgo(query: str, num: int = 5) -> List[Dict[str, str]]:
    driver = msp_search.get_driver()
    return msp_search.search_duckduckgo(driver, query, num)

@mcp.tool()
def search_yandex(query: str, num: int = 5) -> List[Dict[str, str]]:
    driver = msp_search.get_driver()
    return msp_search.search_yandex(driver, query, num)

@mcp.tool()
def get_current_time_and_region() -> dict:
    """
    Return the current local date/time and a region string.
    """
    return msp_date_time.get_current_time_and_region()

def main():
    mcp.run()

if __name__ == "__main__":
    main()

