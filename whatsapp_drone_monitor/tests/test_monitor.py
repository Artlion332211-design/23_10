import tempfile
import unittest
from pathlib import Path

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
        chats={"Шмель": "Шмель"},
        admin_chat="Ви",
        spreadsheet_id="fake",
        state_file=str(Path(tmp_dir) / "state.json"),
    )


def _registry():
    m4e = FakeWorksheet("DJI Matrice 4E", [
        HEADER,
        ["DJI Matrice 4E", "1581F7FVC25AC00DTHLW", "Шмель", "облікований", "В роботі", "01.07.2026", ""],
    ])
    summary = FakeWorksheet("На позиції", [["Група", "всього в роботі"], ["Шмель", "3"]])
    return DroneRegistry(FakeSpreadsheet([m4e, summary]))


INVENTORY_MESSAGE = (
    'Група "Шмель"\n'
    "Matrice 4E\n"
    "1581F7FVC25AC00DTHLW - (Знімаємо на ремонт)\n"
)


class MonitorSingleCycleTests(unittest.TestCase):
    def test_repair_message_updates_sheet_and_alerts_admin(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            registry = _registry()
            client = FakeWhatsAppClient(incoming={"Шмель": [INVENTORY_MESSAGE]})
            monitor = Monitor(config, registry=registry, client=client)

            monitor._poll_once()

            row = registry.find_by_serial("1581F7FVC25AC00DTHLW")[0].worksheet.get_all_values()[1]
            self.assertEqual(row[4], "потрібен ремонт")
            self.assertIn("Знімаємо на ремонт", row[6])

            self.assertEqual(len(client.sent), 1)
            admin_chat, alert_text = client.sent[0]
            self.assertEqual(admin_chat, "Ви")
            self.assertIn("1581F7FVC25AC00DTHLW", alert_text)

            self.assertEqual(len(monitor.state.events), 1)
            self.assertEqual(monitor.state.events[0].event_type, "repair")

    def test_unmatched_serial_alerts_without_touching_sheet(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            registry = _registry()
            unknown_message = "0000000000000000ZZZZ - (ремонт)"  # немає такого серійника в реєстрі
            client = FakeWhatsAppClient(incoming={"Шмель": [unknown_message]})
            monitor = Monitor(config, registry=registry, client=client)

            monitor._poll_once()

            self.assertEqual(len(monitor.state.events), 1)
            self.assertEqual(monitor.state.events[0].event_type, "not_found")
            admin_chat, alert_text = client.sent[0]
            self.assertIn("Не знайдено", alert_text)

    def test_report_command_reads_live_position_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            registry = _registry()
            client = FakeWhatsAppClient(incoming={"Ви": ["звіт"]})
            monitor = Monitor(config, registry=registry, client=client)

            monitor._poll_admin_commands()

            self.assertEqual(len(client.sent), 1)
            admin_chat, report_text = client.sent[0]
            self.assertIn("Шмель", report_text)
            self.assertIn("3 у роботі", report_text)


if __name__ == "__main__":
    unittest.main()
