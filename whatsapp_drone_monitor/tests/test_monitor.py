import tempfile
import unittest
from datetime import datetime as real_datetime
from pathlib import Path
from unittest.mock import patch

from src.config import Config
from src.monitor import Monitor
from src.sheets_client import DroneRegistry
from tests.test_sheets_client import FakeSpreadsheet, FakeWorksheet, HEADER


class FakeWhatsAppClient:
    def __init__(self, incoming):
        # {chat_name: [список повідомлень, які прийдуть по черзі опитувань]}
        self._incoming = incoming
        self.sent = []  # [(chat, text), ...]
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def fetch_new_messages(self, chat_name, last_seen_text):
        messages = self._incoming.get(chat_name, [])
        if not messages:
            return [], last_seen_text
        return messages, messages[-1]

    def send_message(self, chat_name, text):
        self.sent.append((chat_name, text))


def _config(tmp_dir: str) -> Config:
    return Config(
        chats=["Облік БпЛА РР"],
        admin_chat="Archi",
        spreadsheet_id="fake",
        state_file=str(Path(tmp_dir) / "state.json"),
        daily_report_groups=["Кобра", "Шмель"],
    )


def _registry():
    m4e = FakeWorksheet("DJI Matrice 4E", [
        HEADER,
        ["DJI Matrice 4E", "1581F7FVC25AC00DTHLW", "Шмель", "облікований", "В роботі", "01.07.2026", ""],
    ])
    summary = FakeWorksheet("На позиції", [
        ["Група", "Денні борти", "Нічні борти", "Потрібен ремонт", "всього в роботі"],
        ["Кобра", "2", "3", "0", "5"],
        ["Шмєль", "2", "2", "0", "4"],
    ])
    lost = FakeWorksheet("ВТРАЧЕНО", [HEADER])
    return DroneRegistry(FakeSpreadsheet([m4e, summary, lost]))


INVENTORY_MESSAGE = (
    'Група "Шмель"\n'
    "Matrice 4E\n"
    "1581F7FVC25AC00DTHLW - (Знімаємо на ремонт)\n"
)

LOSS_MESSAGE = (
    'Група: "Кобра 1"\n'
    "Пілот: Сурговський А.\n"
    "Втрата борта:\n"
    "М4Т\n"
    "1581F7K3C265S00DFSGJ\n"
    "Дата 20.08.26\n"
    "Час 23:45\n"
    "Орієнтовні координати:\n"
    "37U CP 97930 82698\n"
    "Причина: Почало трусити камеру і сів на дерево\n"
)


class MonitorRepairEventTests(unittest.TestCase):
    def test_repair_message_updates_sheet_and_alerts_admin(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            registry = _registry()
            client = FakeWhatsAppClient(incoming={"Облік БпЛА РР": [INVENTORY_MESSAGE]})
            monitor = Monitor(config, registry=registry, client=client)

            monitor._poll_once()

            row = registry.find_by_serial("1581F7FVC25AC00DTHLW")[0].worksheet.get_all_values()[1]
            self.assertEqual(row[4], "потрібен ремонт")
            self.assertIn("Знімаємо на ремонт", row[6])

            self.assertEqual(len(client.sent), 1)
            admin_chat, alert_text = client.sent[0]
            self.assertEqual(admin_chat, "Archi")
            self.assertIn("1581F7FVC25AC00DTHLW", alert_text)
            self.assertEqual(monitor.state.events[0].event_type, "repair")

    def test_ambiguous_repair_serial_alerts_without_touching_sheet(self):
        # Пілот написав повний, коректний серійник одного борта — але в
        # реєстрі його останні 5 символів випадково збігаються з ІНШИМ
        # реальним бортом на іншому листі, тож бот з обережності не чіпає
        # жоден із двох, а не вгадує.
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            m4t = FakeWorksheet("A", [HEADER, ["M", "1111111111111112ZZZZ", "Шмєль", "so", "В роботі", "", ""]])
            m4e = FakeWorksheet("B", [HEADER, ["M", "2222222222222222ZZZZ", "Кобра", "so", "В роботі", "", ""]])
            registry = DroneRegistry(FakeSpreadsheet([m4t, m4e]))
            client = FakeWhatsAppClient(
                incoming={"Облік БпЛА РР": ["1111111111111112ZZZZ - (ремонт)"]}
            )
            monitor = Monitor(config, registry=registry, client=client)

            monitor._poll_once()

            self.assertEqual(monitor.state.events[0].event_type, "ambiguous")
            self.assertIn("Кілька збігів", client.sent[0][1])
            self.assertEqual(m4t.get_all_values()[1][4], "В роботі")
            self.assertEqual(m4e.get_all_values()[1][4], "В роботі")

    def test_unmatched_repair_serial_alerts_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            registry = _registry()
            unknown_message = "0000000000000000ZZZZ - (ремонт)"
            client = FakeWhatsAppClient(incoming={"Облік БпЛА РР": [unknown_message]})
            monitor = Monitor(config, registry=registry, client=client)

            monitor._poll_once()

            self.assertEqual(monitor.state.events[0].event_type, "not_found")
            self.assertIn("Не знайдено", client.sent[0][1])


class MonitorLossEventTests(unittest.TestCase):
    def test_loss_message_appends_to_loss_log_even_when_not_in_model_sheets(self):
        # Реалістичний випадок: втрачений борт часто взагалі відсутній у
        # листах моделей (він там і не був "1581F7K3C265S00DFSGJ" відсутній
        # у фейковому реєстрі) — журнал втрат все одно має отримати запис,
        # і це НЕ має позначатись як "not_found"/помилка.
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            registry = _registry()
            client = FakeWhatsAppClient(incoming={"Облік БпЛА РР": [LOSS_MESSAGE]})
            monitor = Monitor(config, registry=registry, client=client)

            monitor._poll_once()

            lost_rows = registry._spreadsheet.worksheet("ВТРАЧЕНО").get_all_values()
            self.assertEqual(len(lost_rows), 2)
            self.assertEqual(lost_rows[1][1], "1581F7K3C265S00DFSGJ")
            self.assertEqual(lost_rows[1][4], "втрачено")
            self.assertIn("Сурговський", lost_rows[1][6])

            self.assertEqual(monitor.state.events[0].event_type, "loss")
            self.assertNotEqual(monitor.state.events[0].event_type, "not_found")
            self.assertIn("1581F7K3C265S00DFSGJ", client.sent[0][1])


class MonitorAdminCommandTests(unittest.TestCase):
    def test_position_command_reads_live_day_night_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            registry = _registry()
            client = FakeWhatsAppClient(incoming={"Archi": ["на позиції"]})
            monitor = Monitor(config, registry=registry, client=client)

            monitor._poll_admin_commands()

            self.assertEqual(len(client.sent), 1)
            _, text = client.sent[0]
            self.assertIn("Кобра", text)
            self.assertIn("Шмель", text)
            self.assertIn("денних", text)
            self.assertIn("нічних", text)

    def test_report_command_includes_recent_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            registry = _registry()
            client = FakeWhatsAppClient(incoming={"Archi": ["звіт"]})
            monitor = Monitor(config, registry=registry, client=client)
            monitor.state.record_event(event_type="loss", serial="ABC123", group="Кобра")

            monitor._poll_admin_commands()

            _, text = client.sent[0]
            self.assertIn("втрат", text.lower())


class DailyReportTimingTests(unittest.TestCase):
    def test_no_report_before_target_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            client = FakeWhatsAppClient(incoming={})
            monitor = Monitor(config, registry=_registry(), client=client)
            with patch("src.monitor.datetime") as mock_dt:
                mock_dt.now.return_value = real_datetime(2026, 8, 22, 20, 59)
                monitor._maybe_send_daily_report()
            self.assertEqual(client.sent, [])

    def test_sends_once_per_day_after_target_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            client = FakeWhatsAppClient(incoming={})
            monitor = Monitor(config, registry=_registry(), client=client)
            with patch("src.monitor.datetime") as mock_dt:
                mock_dt.now.return_value = real_datetime(2026, 8, 22, 21, 5)
                monitor._maybe_send_daily_report()
                monitor._maybe_send_daily_report()
            self.assertEqual(len(client.sent), 1)

    def test_sends_again_next_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            client = FakeWhatsAppClient(incoming={})
            monitor = Monitor(config, registry=_registry(), client=client)
            with patch("src.monitor.datetime") as mock_dt:
                mock_dt.now.return_value = real_datetime(2026, 8, 22, 21, 5)
                monitor._maybe_send_daily_report()
                mock_dt.now.return_value = real_datetime(2026, 8, 23, 21, 5)
                monitor._maybe_send_daily_report()
            self.assertEqual(len(client.sent), 2)


if __name__ == "__main__":
    unittest.main()
