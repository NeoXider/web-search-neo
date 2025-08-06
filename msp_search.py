import logging
import urllib.parse
from typing import List, Dict

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
import driver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

def get_driver() -> webdriver.Chrome:
    """Поднимаем headless Chrome через webdriver-manager."""
    return driver.get_driver(log)


def search_google(driver: webdriver.Chrome, query: str, num: int = 5) -> List[Dict[str, str]]:
    """Ищем в Google, парсим заголовок из <h3>, ссылку из родительского <a>, сниппет — из соседних DIV."""
    url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query) + "&hl=en"
    driver.get(url)
    results = []
    items = driver.find_elements(By.CSS_SELECTOR, "div.g")
    for elem in items[:num]:
        try:
            h3 = elem.find_element(By.TAG_NAME, "h3")
            a = h3.find_element(By.XPATH, "./ancestor::a")
            title = h3.text
            link = a.get_attribute("href")
            # сниппет может быть в разных div’ах
            snippet = ""
            for sel in ("div.IsZvec", "div.VwiC3b", "span.aCOpRe"):
                try:
                    snippet = elem.find_element(By.CSS_SELECTOR, sel).text
                    if snippet:
                        break
                except:
                    continue
            results.append({"title": title, "url": link, "snippet": snippet})
        except Exception:
            continue
    return results


def search_yandex(driver: webdriver.Chrome, query: str, num: int = 5) -> List[Dict[str, str]]:
    """Ищем в Яндексе, парсим из h2 > a и div.text."""
    url = "https://yandex.ru/search/?text=" + urllib.parse.quote_plus(query)
    driver.get(url)
    results = []
    items = driver.find_elements(By.CSS_SELECTOR, "li.serp-item")
    for item in items[:num]:
        try:
            a = item.find_element(By.CSS_SELECTOR, "h2 a, a.link")
            title = a.text
            link = a.get_attribute("href")
            snippet = ""
            try:
                snippet = item.find_element(By.CSS_SELECTOR, "div.text, .organic__snippet").text
            except:
                pass
            results.append({"title": title, "url": link, "snippet": snippet})
        except Exception:
            continue
    return results


def search_duckduckgo(driver: webdriver.Chrome, query: str, num: int = 5) -> List[Dict[str, str]]:
    """Ищем в DuckDuckGo (HTML-версия), парсим из div.result."""
    url = "https://duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query)
    driver.get(url)
    results = []
    items = driver.find_elements(By.CSS_SELECTOR, "div.result")
    for item in items[:num]:
        try:
            a = item.find_element(By.CSS_SELECTOR, "a.result__a")
            title = a.text
            link = a.get_attribute("href")
            snippet = ""
            try:
                snippet = item.find_element(By.CSS_SELECTOR, "div.result__snippet, a.result__snippet").text
            except:
                pass
            results.append({"title": title, "url": link, "snippet": snippet})
        except Exception:
            continue
    return results


def main():
    query = "Python programming"
    num_results = 5

    driver = get_driver()
    try:
        for name, func in [
            ("Google", search_google),
            ("Yandex", search_yandex),
            ("DuckDuckGo", search_duckduckgo),
        ]:
            log.info(f"Searching with {name}…")
            res = func(driver, query, num_results)
            print(f"\n=== {name.upper()} — {len(res)} результатов ===")
            for idx, hit in enumerate(res, 1):
                print(f"{idx}. {hit['title']}\n   {hit['url']}\n   {hit['snippet']}\n")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
