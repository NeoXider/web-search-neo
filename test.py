# auto_test_msp_server.py

import unittest
from typing import List, Dict

from main import (
    fetch_url_text,
    fetch_page_links,
    google_search_tool,
    yandex_search_tool,
    duckduckgo_search_tool,
)
from selenium.common.exceptions import WebDriverException

class TestMspServer(unittest.TestCase):
    # ------------------------------------------------------------------
    # 1. Список URL-ов для проверки
    # ------------------------------------------------------------------
    urls = [
        "https://example.com",
        "https://www.python.org",
        "https://httpbin.org/html",
    ]

    # ------------------------------------------------------------------
    # 2. Тест fetch_url_text
    # ------------------------------------------------------------------
    def test_fetch_url_text(self):
        for url in self.urls:
            with self.subTest(url=url):
                text = fetch_url_text(url)
                self.assertIsInstance(text, str, f"fetch_url_text did not return str for {url}")
                self.assertGreater(len(text.strip()), 0, f"No text extracted from {url}")

    # ------------------------------------------------------------------
    # 3. Тест fetch_page_links
    # ------------------------------------------------------------------
    def test_fetch_page_links(self):
        for url in self.urls:
            with self.subTest(url=url):
                links = fetch_page_links(url)
                self.assertIsInstance(links, list, f"fetch_page_links did not return list for {url}")
                for link in links:
                    self.assertIsInstance(link, str, f"Non-str item in links for {url}: {link!r}")

    # ------------------------------------------------------------------
    # 4. Тест google_search_tool
    # ------------------------------------------------------------------
    def test_google_search_tool(self):
        query = "Python programming"
        try:
            results = google_search_tool(query, num=3)
        except WebDriverException as e:
            self.skipTest(f"Skipping Google search test due to WebDriver error: {e}")
        self.assertIsInstance(results, list, "google_search_tool did not return a list")
        if results:
            first: Dict[str, str] = results[0]
            for key in ("title", "url", "snippet"):
                self.assertIn(key, first, f"Missing key '{key}' in first google_search_tool result")
                self.assertIsInstance(first[key], str, f"Expected string for '{key}', got {type(first[key])}")

    # ------------------------------------------------------------------
    # 5. Тест yandex_search_tool
    # ------------------------------------------------------------------
    def test_yandex_search_tool(self):
        query = "Python programming"
        try:
            results = yandex_search_tool(query, num=3)
        except WebDriverException as e:
            self.skipTest(f"Skipping Yandex search test due to WebDriver error: {e}")
        self.assertIsInstance(results, list, "yandex_search_tool did not return a list")
        if results:
            first: Dict[str, str] = results[0]
            for key in ("title", "url", "snippet"):
                self.assertIn(key, first, f"Missing key '{key}' in first yandex_search_tool result")
                self.assertIsInstance(first[key], str, f"Expected string for '{key}', got {type(first[key])}")

    # ------------------------------------------------------------------
    # 6. Тест duckduckgo_search_tool
    # ------------------------------------------------------------------
    def test_duckduckgo_search_tool(self):
        query = "Python programming"
        try:
            results = duckduckgo_search_tool(query, num=3)
        except WebDriverException as e:
            self.skipTest(f"Skipping DuckDuckGo search test due to WebDriver error: {e}")
        self.assertIsInstance(results, list, "duckduckgo_search_tool did not return a list")
        if results:
            first: Dict[str, str] = results[0]
            for key in ("title", "url", "snippet"):
                self.assertIn(key, first, f"Missing key '{key}' in first duckduckgo_search_tool result")
                self.assertIsInstance(first[key], str, f"Expected string for '{key}', got {type(first[key])}")

if __name__ == "__main__":
    unittest.main(verbosity=2)
