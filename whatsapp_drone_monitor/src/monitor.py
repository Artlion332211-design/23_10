from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Optional

from .config import Config
from .parser import EventType, parse_message
from .reporter import format_event_alert, format_status_report
from .sheets_client import DroneRegistry, open_registry
from .state import BotState, load_state, save_state
from .whatsapp_client import WhatsAppClient

logger = logging.getLogger(__name__)

REPORT_COMMANDS = {"звіт", "/звіт", "статус", "/статус", "на позиції", "/на_позиції"}


class Monitor:
    def __init__(self, config: Config, registry: Optional[DroneRegistry] = None, client=None):
        self.config = config
        self.client = client or WhatsAppClient(config.chrome_profile_dir)
        self.registry = registry or open_registry(
            config.spreadsheet_id, config.credentials_path, config.model_sheets
        )
        self.state: BotState = load_state(config.state_file)
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
                self._handle_message(text)
            if new_messages:
                save_state(self.state, self.config.state_file)

        self._poll_admin_commands()

    def _handle_message(self, text: str) -> None:
        for drone_event in parse_message(text):
            new_status = (
                self.config.status_repair
                if drone_event.event_type is EventType.REPAIR
                else self.config.status_lost
            )
            note = _build_note(drone_event)

            matches = self.registry.set_status(drone_event.serial, new_status, note=note)

            if not matches:
                event = self.state.record_event(
                    event_type="not_found",
                    serial=drone_event.serial,
                    group=drone_event.group,
                    note=note,
                )
            else:
                match = matches[0]
                event = self.state.record_event(
                    event_type=drone_event.event_type.value,
                    serial=drone_event.serial,
                    group=drone_event.group,
                    sheet=match.worksheet.title,
                    old_status=match.old_status,
                    new_status=new_status,
                    note=note,
                )
            self._alert(event)

    def _alert(self, event) -> None:
        try:
            self.client.send_message(self.config.admin_chat, format_event_alert(event))
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
            summary = self.registry.read_position_summary(self.config.position_summary_sheet)
        except Exception:
            logger.exception("Не вдалося прочитати лист «%s»", self.config.position_summary_sheet)
            summary = ""
        try:
            self.client.send_message(
                self.config.admin_chat, format_status_report(summary, self.state.events)
            )
        except Exception:
            logger.exception("Не вдалося надіслати звіт адміну")


def _build_note(drone_event) -> str:
    parts = []
    if drone_event.pilot:
        parts.append(f"Пілот: {drone_event.pilot}")
    if drone_event.date or drone_event.time:
        parts.append(f"{drone_event.date or ''} {drone_event.time or ''}".strip())
    if drone_event.coordinates:
        parts.append(f"Координати: {drone_event.coordinates}")
    if drone_event.reason:
        parts.append(f"Причина: {drone_event.reason}")
    if not parts and drone_event.note:
        parts.append(drone_event.note)
    return "; ".join(parts)
