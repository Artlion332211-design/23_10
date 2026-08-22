import tempfile
import unittest
from pathlib import Path

from src.state import BotState, load_state, save_state


class BotStateTests(unittest.TestCase):
    def test_record_event_stores_fields(self):
        state = BotState()
        state.record_event(event_type="repair", serial="ABC123", group="Шмель", new_status="потрібен ремонт")
        self.assertEqual(len(state.events), 1)
        self.assertEqual(state.events[0].serial, "ABC123")

    def test_roundtrip_serialization(self):
        state = BotState()
        state.last_seen["Шмель"] = "остання перевірена фраза"
        state.record_event(event_type="loss", serial="XYZ789", group="Кобра 1")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            save_state(state, path)
            loaded = load_state(path)

        self.assertEqual(loaded.last_seen["Шмель"], "остання перевірена фраза")
        self.assertEqual(len(loaded.events), 1)
        self.assertEqual(loaded.events[0].serial, "XYZ789")

    def test_load_missing_file_returns_empty_state(self):
        state = load_state("/tmp/this-file-does-not-exist-12345.json")
        self.assertEqual(state.events, [])
        self.assertEqual(state.last_seen, {})


if __name__ == "__main__":
    unittest.main()
