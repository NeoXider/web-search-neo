import random
import os
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0.3 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36"
]

def get_driver(log = None) -> webdriver.Chrome:
    """Поднимаем headless Chrome через webdriver-manager с динамическим UA и прокси."""
    opts = Options()
    # Базовые флаги для headless режима
    opts.add_argument("--headless")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-dev-shm-usage")
    # Отключаем признаки автоматизации
    opts.add_argument("--disable-blink-features=AutomationControlled")

    # Генерация случайного User‑Agent, чтобы обходить простые проверки
    ua = random.choice(USER_AGENTS)
    opts.add_argument(f"user-agent={ua}")
    if log is not None:
        log.debug(f"Используем UA: {ua}")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=opts)
