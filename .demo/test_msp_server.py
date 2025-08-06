# auto_test_msp_server.py
import unittest
from msp_server import fetch_url_text, fetch_page_links, google_search


class TestMspServer(unittest.TestCase):
    # ------------------------------------------------------------------
    # 1. Список URL‑ов для проверки
    # ------------------------------------------------------------------
    urls = [
        "https://example.com",
        "https://www.python.org",
        "https://httpbin.org/html",   # простая страница с <h1>, <p> и т.д.
        # Добавьте свои адреса, если нужно
    ]

    # ------------------------------------------------------------------
    # 2. Тест функции fetch_url_text
    # ------------------------------------------------------------------
    def test_fetch_url_text(self):
        for url in self.urls:
            with self.subTest(url=url):
                text = fetch_url_text(url)
                # Проверяем, что текст действительно получен
                self.assertIsInstance(text, str, f"Result is not a string for {url}")
                self.assertGreater(len(text.strip()), 0,
                                  f"No text extracted from {url}")

    # ------------------------------------------------------------------
    # 3. Тест функции fetch_page_links
    # ------------------------------------------------------------------
    def test_fetch_page_links(self):
        for url in self.urls:
            with self.subTest(url=url):
                links = fetch_page_links(url)
                # Проверяем, что возвращается список
                self.assertIsInstance(links, list,
                                      f"Result is not a list for {url}")
                # Если хочется проверить наличие хотя бы одной ссылки:
                if len(self.urls) > 0:   # просто пример – можно убрать
                    # На некоторых страницах может быть 0 ссылок (пример: httpbin.org/html)
                    pass
    # ------------------------------------------------------------------
    # 4. Тест функции google_search
    # ------------------------------------------------------------------
    def test_google_search(self):
        query = "Python programming"
        results = google_search(query)
        self.assertIsInstance(results, list, "Result is not a list for google search")
        if len(results) > 0:
            first = results[0]
            # Check required keys
            for key in ["title", "url", "snippet"]:
                self.assertIn(key, first, f"Missing {key} in result")

# ----------------------------------------------------------------------
# Запуск тестов при прямом вызове файла
# ----------------------------------------------------------------------
if __name__ == "__main__":
    unittest.main(verbosity=2)   # verbosity=2 – более подробный вывод
