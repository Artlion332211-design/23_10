from __future__ import annotations

import logging
import time
from datetime import date, datetime
from datetime import time as dt_time
from typing import Optional

from .config import Config
from .parser import EventType, parse_message
from .reporter import format_daily_report, format_event_alert, format_position_stats
from .sheets_client import AmbiguousSerialError, DroneRegistry, PositionStat, open_registry
from .state import BotState, load_state, save_state
from .whatsapp_client import WhatsAppClient

logger = logging.getLogger(__name__)

POSITION_COMMANDS = {"на позиції", "/на_позиції", "позиції"}
DAILY_REPORT_COMMANDS = {"звіт", "/звіт", "статус", "/статус"}


class Monitor:
    def __init__(self, config: Config, registry: Optional[DroneRegistry] = None, client=None):
        self.config = config
        self.client = client or WhatsAppClient(config.chrome_profile_dir)
        self.registry = registry or open_registry(
            config.spreadsheet_id, config.credentials_path, config.model_sheets, config.serial_suffix_length
        )
        self.state: BotState = load_state(config.state_file)
        self._last_daily_report_date: Optional[date] = None

    def run(self) -> None:
        self.client.start()
        logger.info("Моніторинг запущено. Чати: %s", ", ".join(self.config.chats))
        try:
            while True:
                self._poll_once()
                self._maybe_send_daily_report()
                time.sleep(self.config.poll_interval_seconds)
        except KeyboardInterrupt:
            logger.info("Зупинка за запитом користувача")
        finally:
            save_state(self.state, self.config.state_file)
            self.client.stop()

    def _poll_once(self) -> None:
        for chat_name in self.config.chats:
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
            try:
                self._process_drone_event(drone_event)
            except Exception:
                # Ізольовано на рівні однієї події: тимчасовий збій Google
                # Sheets (мережа, ліміт запитів) не має класти весь цикл
                # опитування й лишати необроблені повідомлення непроглянутими.
                logger.exception("Не вдалося обробити подію для серійника %s", drone_event.serial)

    def _process_drone_event(self, drone_event) -> None:
        note = _build_note(drone_event)

        if drone_event.event_type is EventType.LOSS:
            event = self._process_loss(drone_event, note)
        else:
            event = self._process_repair(drone_event, note)

        save_state(self.state, self.config.state_file)
        self._send(format_event_alert(event))

    def _process_loss(self, drone_event, note: str):
        # Живі дані показують, що втрачений борт часто взагалі відсутній у
        # листах моделей — журнал "ВТРАЧЕНО" це окремий запис, не заміна
        # статусу активного борта. Тому пишемо в обидва місця: якщо серійник
        # ще значиться активним десь у флоті — знімаємо його звідти, і
        # завжди додаємо повний запис у журнал втрат.
        try:
            matches = self.registry.set_status(drone_event.serial, self.config.status_lost, note=note)
        except AmbiguousSerialError:
            # Неоднозначність у листах моделей не має блокувати головне —
            # запис про втрату в журнал все одно додається нижче.
            logger.warning("Неоднозначний серійник %s при втраті — статус у листах моделей не чіпаємо", drone_event.serial)
            matches = []
        self.registry.append_loss_record(
            self.config.loss_log_sheet,
            model=drone_event.model,
            serial=drone_event.serial,
            group=drone_event.group,
            status=self.config.status_lost,
            note=note,
        )
        return self.state.record_event(
            event_type="loss",
            serial=drone_event.serial,
            group=drone_event.group,
            sheet=matches[0].worksheet.title if matches else self.config.loss_log_sheet,
            old_status=matches[0].old_status if matches else None,
            new_status=self.config.status_lost,
            note=note,
        )

    def _process_repair(self, drone_event, note: str):
        try:
            matches = self.registry.set_status(drone_event.serial, self.config.status_repair, note=note)
        except AmbiguousSerialError as exc:
            return self.state.record_event(
                event_type="ambiguous",
                serial=drone_event.serial,
                group=drone_event.group,
                note=f"Можливі борти: {', '.join(exc.candidates)}" + (f" | {note}" if note else ""),
            )
        if not matches:
            return self.state.record_event(
                event_type="not_found", serial=drone_event.serial, group=drone_event.group, note=note,
            )
        match = matches[0]
        return self.state.record_event(
            event_type="repair",
            serial=drone_event.serial,
            group=drone_event.group,
            sheet=match.worksheet.title,
            old_status=match.old_status,
            new_status=self.config.status_repair,
            note=note,
        )

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
            command = text.strip().lower()
            if command in POSITION_COMMANDS:
                self._send_position_snapshot()
            elif command in DAILY_REPORT_COMMANDS:
                self._send_daily_report()

    def _send_position_snapshot(self) -> None:
        stats = self._read_position_stats()
        self._send("📍 На позиції зараз:\n" + format_position_stats(stats))

    def _send_daily_report(self) -> None:
        stats = self._read_position_stats()
        self._send(format_daily_report(stats, self.state.events))

    def _read_position_stats(self):
        try:
            return self.registry.read_position_stats(
                self.config.daily_report_groups, self.config.position_summary_sheet
            )
        except Exception:
            logger.exception("Не вдалося прочитати лист «%s»", self.config.position_summary_sheet)
            return [PositionStat(group=g, found=False) for g in self.config.daily_report_groups]

    def _maybe_send_daily_report(self) -> None:
        now = datetime.now()
        hours, minutes = self.config.daily_report_time.split(":")
        target = dt_time(hour=int(hours), minute=int(minutes))
        if now.time() < target:
            return
        if self._last_daily_report_date == now.date():
            return
        self._send_daily_report()
        self._last_daily_report_date = now.date()

    def _send(self, text: str) -> None:
        try:
            self.client.send_message(self.config.admin_chat, text)
        except Exception:
            logger.exception("Не вдалося надіслати повідомлення адміну")


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
