from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Optional

from .config import Config
from .parser import EventType, classify
from .reporter import format_alert, format_status_report
from .state import FleetState, load_state, save_state
from .whatsapp_client import WhatsAppClient

logger = logging.getLogger(__name__)

REPORT_COMMANDS = {"звіт", "/звіт", "статус", "/статус"}


class Monitor:
    def __init__(self, config: Config):
        self.config = config
        self.client = WhatsAppClient(config.chrome_profile_dir)
        self.state: FleetState = load_state(config.state_file)
        self._last_report_at: Optional[datetime] = None

    def run(self) -> None:
        self.client.start()
        logger.info("Моніторинг запущено. Чати: %s", ", ".join(self.config.chats))
        try:
            while True:
                self._poll_once()
                self._maybe_send_periodic_report()
                time.sleep(self.config.poll_interval_seconds)
        except KeyboardInterrupt:
            logger.info("Зупинка за запитом користувача")
        finally:
            save_state(self.state, self.config.state_file)
            self.client.stop()

    def _poll_once(self) -> None:
        for chat_name, group_label in self.config.chats.items():
            try:
                new_messages, last_text = self.client.fetch_new_messages(
                    chat_name, self.state.last_seen.get(chat_name)
                )
            except Exception:
                logger.exception("Не вдалося прочитати чат %s", chat_name)
                continue

            if last_text is not None:
                self.state.last_seen[chat_name] = last_text
            for text in new_messages:
                self._handle_message(group_label, text)
            if new_messages:
                save_state(self.state, self.config.state_file)

        self._poll_admin_commands()

    def _handle_message(self, group_label: str, text: str) -> None:
        classified = classify(text, self.config)

        if classified.event_type is EventType.LAUNCH:
            self.state.record_launch(group_label)
        elif classified.event_type is EventType.RETURN:
            self.state.record_return(group_label)
        elif classified.event_type is EventType.LOSS:
            self.state.record_loss(group_label, text, classified.matched_keyword)
            self._alert("Втрата дрона", group_label, text)
        elif classified.event_type is EventType.INCIDENT:
            self.state.record_incident(group_label, text, classified.matched_keyword)
            self._alert("Нештатна ситуація", group_label, text)

    def _alert(self, label: str, group_label: str, text: str) -> None:
        try:
            self.client.send_message(self.config.admin_chat, format_alert(label, group_label, text))
        except Exception:
            logger.exception("Не вдалося надіслати алерт адміну")

    def _poll_admin_commands(self) -> None:
        try:
            new_messages, last_text = self.client.fetch_new_messages(
                self.config.admin_chat, self.state.last_seen.get(self.config.admin_chat)
            )
        except Exception:
            logger.exception("Не вдалося прочитати команди адміна")
            return

        if last_text is not None:
            self.state.last_seen[self.config.admin_chat] = last_text
        for text in new_messages:
            if text.strip().lower() in REPORT_COMMANDS:
                self._send_report()

    def _maybe_send_periodic_report(self) -> None:
        now = datetime.now()
        interval = timedelta(hours=self.config.report_interval_hours)
        if self._last_report_at is None or now - self._last_report_at >= interval:
            self._send_report()
            self._last_report_at = now

    def _send_report(self) -> None:
        try:
            self.client.send_message(self.config.admin_chat, format_status_report(self.state))
        except Exception:
            logger.exception("Не вдалося надіслати звіт адміну")
