from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import List, Optional, Tuple

from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

logger = logging.getLogger(__name__)

WHATSAPP_URL = "https://web.whatsapp.com"

# WhatsApp Web змінює розмітку без попередження — якщо бот перестав бачити
# чати чи повідомлення, першим ділом онови саме ці селектори (F12 -> Inspect
# у браузері на живій сторінці web.whatsapp.com).
SELECTORS = {
    "chat_list": "div[aria-label='Chat list'], div[data-testid='chat-list']",
    "search_box": "div[contenteditable='true'][data-tab='3'], div[title='Search input textbox']",
    "search_result": "div[aria-label='Chat list'] span[title='{name}']",
    "message_in": "div.message-in, div[data-testid='msg-container'].message-in",
    "message_text": "span.selectable-text.copyable-text",
    "compose_box": "footer div[contenteditable='true'][data-tab='10'], footer div[contenteditable='true']",
}


class WhatsAppClient:
    """Тонка обгортка над WhatsApp Web через Selenium.

    Це неофіційна автоматизація особистого WhatsApp (не Business Cloud API):
    сесія тримається на профілі Chrome, вхід — одноразовим скануванням QR.
    Через це існує ризик, що номер буде позначено як підозрілий за
    надмірну автоматичну активність — див. застереження в README.
    """

    def __init__(self, profile_dir: str, headless: bool = False):
        self._profile_dir = Path(profile_dir).resolve()
        self._headless = headless
        self._driver: Optional[webdriver.Chrome] = None

    def start(self, login_timeout: int = 120) -> None:
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        options = Options()
        options.add_argument(f"user-data-dir={self._profile_dir}")
        options.add_argument("--profile-directory=Default")
        if self._headless:
            options.add_argument("--headless=new")
        options.add_argument("--disable-notifications")
        self._driver = webdriver.Chrome(options=options)
        self._driver.get(WHATSAPP_URL)

        wait = WebDriverWait(self._driver, login_timeout)
        try:
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, SELECTORS["chat_list"])))
            logger.info("Увійшли в WhatsApp Web (збережена сесія профілю)")
        except TimeoutException:
            logger.info("Відскануй QR-код у вікні Chrome протягом %s с", login_timeout)
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, SELECTORS["chat_list"])))
            logger.info("Вхід виконано, сесія збережена в %s", self._profile_dir)

    def stop(self) -> None:
        if self._driver is not None:
            self._driver.quit()
            self._driver = None

    def open_chat(self, chat_name: str, timeout: int = 15) -> None:
        if self._driver is None:
            raise RuntimeError("Клієнт не запущено — виклич start()")
        wait = WebDriverWait(self._driver, timeout)
        search = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, SELECTORS["search_box"])))
        search.click()
        search.send_keys(Keys.CONTROL, "a")
        search.send_keys(chat_name)
        time.sleep(1.5)  # список чатів фільтрується асинхронно
        result = wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, SELECTORS["search_result"].format(name=chat_name)))
        )
        result.click()
        search.send_keys(Keys.ESCAPE)

    def fetch_new_messages(
        self, chat_name: str, last_seen_text: Optional[str]
    ) -> Tuple[List[str], Optional[str]]:
        """Повертає (нові вхідні повідомлення, текст останнього повідомлення в чаті).

        WhatsApp Web не віддає через DOM стабільний ID повідомлення, тому як
        маркер "вже оброблено" використовується текст останнього побаченого
        повідомлення. Це не ідеально при кількох підряд однакових текстах,
        але достатньо надійно при короткому інтервалі опитування.
        """
        if self._driver is None:
            raise RuntimeError("Клієнт не запущено — виклич start()")
        self.open_chat(chat_name)
        time.sleep(1.0)

        try:
            nodes = self._driver.find_elements(By.CSS_SELECTOR, SELECTORS["message_in"])
        except StaleElementReferenceException:
            return [], last_seen_text

        texts: List[str] = []
        for node in nodes:
            try:
                spans = node.find_elements(By.CSS_SELECTOR, SELECTORS["message_text"])
                text = " ".join(s.text for s in spans if s.text).strip()
            except StaleElementReferenceException:
                continue
            if text:
                texts.append(text)

        if not texts:
            return [], last_seen_text

        if last_seen_text is None:
            # Перший запуск для цього чату: не ретранслюємо всю історію
            # заднім числом, інакше кожен рестарт бота засипле адміна старим.
            return [], texts[-1]

        if last_seen_text in texts:
            idx = len(texts) - 1 - texts[::-1].index(last_seen_text)
            new_texts = texts[idx + 1 :]
        else:
            new_texts = texts

        return new_texts, texts[-1]

    def send_message(self, chat_name: str, text: str, timeout: int = 15) -> None:
        if self._driver is None:
            raise RuntimeError("Клієнт не запущено — виклич start()")
        self.open_chat(chat_name, timeout=timeout)
        wait = WebDriverWait(self._driver, timeout)
        box = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, SELECTORS["compose_box"])))
        box.click()

        lines = text.split("\n")
        for i, line in enumerate(lines):
            box.send_keys(line)
            if i < len(lines) - 1:
                box.send_keys(Keys.SHIFT, Keys.ENTER)
        box.send_keys(Keys.ENTER)
