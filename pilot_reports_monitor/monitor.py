"""
Моніторинг звітів пілотів у групі WhatsApp.

Пілоти пишуть у групу повідомлення, що починається з їхнього позивного
(наприклад: "Шмель 4 вильоти" або "Кобра: 6"). Скрипт читає нові
повідомлення групи через WhatsApp Web (звичайний акаунт, без Business API),
запам'ятовує час останнього звіту кожної позиції і, якщо позиція не
подавала звіт довше report_timeout_hours (за замовчуванням 24 год),
надсилає адміну попередження в окремий чат.

Запуск:
    pip install -r requirements.txt
    python monitor.py
    (у відкритому вікні Chrome один раз відсканувати QR-код)

Налаштування — у файлі config.json поруч зі скриптом.
"""

import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

BASE_DIR = Path(__file__).resolve().parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("pilot_reports_monitor")


def load_config():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE_DIR / "config.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["chrome_profile_dir"] = str((BASE_DIR / cfg["chrome_profile_dir"]).resolve())
    cfg["state_file"] = str((BASE_DIR / cfg["state_file"]).resolve())
    return cfg


def load_state(cfg):
    path = Path(cfg["state_file"])
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        data["seen_ids"] = set(data.get("seen_ids", []))
        return data
    return {
        "seen_ids": set(),
        "last_report": {},          # позивний -> {"time": iso, "count": int|None, "text": str}
        "alerted_since_report": {}, # позивний -> iso часу останнього алерту
        "started_at": datetime.now(timezone.utc).isoformat(),
    }


def save_state(cfg, state):
    path = Path(cfg["state_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    data = dict(state)
    data["seen_ids"] = list(state["seen_ids"])[-5000:]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_report(text, positions):
    """Визначає позицію та кількість вильотів у тексті повідомлення.

    Позиція розпізнається лише якщо один з її позивних стоїть на самому
    початку повідомлення (щоб не спрацьовувати на випадкову згадку в тексті).
    """
    normalized = text.strip().lower()
    for pos in positions:
        for alias in pos["aliases"]:
            alias_l = alias.lower()
            if not normalized.startswith(alias_l):
                continue
            rest = normalized[len(alias_l):]
            if rest and rest[0].isalnum():
                continue  # це частина довшого слова, а не позивний
            match = re.search(r"\d+", rest)
            count = int(match.group()) if match else None
            return pos["name"], count
    return None


def build_driver(cfg):
    options = webdriver.ChromeOptions()
    Path(cfg["chrome_profile_dir"]).mkdir(parents=True, exist_ok=True)
    options.add_argument(f"--user-data-dir={cfg['chrome_profile_dir']}")
    options.add_argument("--profile-directory=Default")
    if cfg.get("headless"):
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1200,900")
    driver = webdriver.Chrome(options=options)
    driver.get("https://web.whatsapp.com")
    return driver


def wait_for_login(driver, timeout=180):
    log.info("Очікування входу в WhatsApp Web (за потреби відскануйте QR-код)...")
    try:
        WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.ID, "side")))
    except TimeoutException as exc:
        raise RuntimeError(
            "Не вдалося увійти в WhatsApp Web за відведений час. "
            "Відскануйте QR-код у вікні браузера і перезапустіть скрипт."
        ) from exc
    log.info("Вхід виконано.")


def open_chat(driver, chat_name, timeout=20):
    side = WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.ID, "side")))
    search_box = side.find_element(By.CSS_SELECTOR, "div[contenteditable='true']")
    search_box.click()
    search_box.send_keys(Keys.CONTROL, "a")
    search_box.send_keys(Keys.DELETE)
    search_box.send_keys(chat_name)
    time.sleep(1.5)
    result = WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, f'span[title="{chat_name}"]'))
    )
    result.click()
    time.sleep(1.0)
    search_box.send_keys(Keys.ESCAPE)


def scan_messages(driver):
    rows = driver.find_elements(By.CSS_SELECTOR, "div.copyable-text")
    messages = []
    for row in rows:
        msg_id = row.get_attribute("data-id")
        if not msg_id:
            continue
        spans = row.find_elements(By.CSS_SELECTOR, "span.selectable-text")
        text = " ".join(s.text for s in spans).strip()
        if text:
            messages.append((msg_id, text))
    return messages


def send_message(driver, chat_name, text, timeout=20):
    open_chat(driver, chat_name, timeout)
    main = driver.find_element(By.ID, "main")
    compose = main.find_element(By.CSS_SELECTOR, "footer div[contenteditable='true']")
    compose.click()
    lines = text.split("\n")
    for i, line in enumerate(lines):
        compose.send_keys(line)
        if i < len(lines) - 1:
            compose.send_keys(Keys.SHIFT, Keys.ENTER)
    compose.send_keys(Keys.ENTER)
    log.info("Надіслано повідомлення в чат '%s'.", chat_name)


def process_messages(messages, cfg, state):
    now_iso = datetime.now(timezone.utc).isoformat()
    for msg_id, text in messages:
        if msg_id in state["seen_ids"]:
            continue
        state["seen_ids"].add(msg_id)
        result = parse_report(text, cfg["positions"])
        if result:
            pos_name, count = result
            state["last_report"][pos_name] = {
                "time": now_iso,
                "count": count,
                "text": text[:200],
            }
            state["alerted_since_report"].pop(pos_name, None)
            log.info("Звіт від '%s' (вильотів: %s): %s", pos_name, count, text[:120])


def format_dt(dt):
    return dt.astimezone().strftime("%d.%m %H:%M")


def check_and_alert(driver, cfg, state):
    now = datetime.now(timezone.utc)
    timeout = timedelta(hours=cfg["report_timeout_hours"])
    repeat = timedelta(hours=cfg["alert_repeat_hours"])
    started_at = datetime.fromisoformat(state["started_at"])

    overdue = []
    for pos in cfg["positions"]:
        name = pos["name"]
        last = state["last_report"].get(name)
        last_time = datetime.fromisoformat(last["time"]) if last else started_at
        if now - last_time <= timeout:
            continue
        last_alert = state["alerted_since_report"].get(name)
        if last_alert and now - datetime.fromisoformat(last_alert) < repeat:
            continue
        overdue.append((name, last_time))

    if not overdue:
        return

    lines = [f"⚠️ Немає звіту понад {cfg['report_timeout_hours']} год:"]
    for name, last_time in overdue:
        lines.append(f"- {name}: останній звіт {format_dt(last_time)}")
        state["alerted_since_report"][name] = now.isoformat()

    send_message(driver, cfg["admin_chat_name"], "\n".join(lines))


def main():
    cfg = load_config()
    state = load_state(cfg)

    driver = build_driver(cfg)
    wait_for_login(driver)
    open_chat(driver, cfg["group_name"])
    log.info("Спостерігаю за групою '%s'.", cfg["group_name"])

    last_alert_check = 0.0
    try:
        while True:
            try:
                messages = scan_messages(driver)
                process_messages(messages, cfg, state)
                save_state(cfg, state)

                now = time.time()
                if now - last_alert_check >= cfg["alert_check_interval_seconds"]:
                    check_and_alert(driver, cfg, state)
                    save_state(cfg, state)
                    open_chat(driver, cfg["group_name"])
                    last_alert_check = now
            except Exception:
                log.exception("Помилка в основному циклі, продовжую роботу.")
            time.sleep(cfg["scan_interval_seconds"])
    except KeyboardInterrupt:
        log.info("Зупинено користувачем.")
    finally:
        save_state(cfg, state)
        driver.quit()


if __name__ == "__main__":
    main()
