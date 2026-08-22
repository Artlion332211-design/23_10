import tempfile
import unittest
from pathlib import Path

from src.state import FleetState, load_state, save_state


class FleetStateTests(unittest.TestCase):
    def test_launch_increments_on_position(self):
        state = FleetState()
        state.record_launch("ШМЕЛЬ")
        state.record_launch("ШМЕЛЬ")
        self.assertEqual(state.group("ШМЕЛЬ").on_position, 2)
        self.assertEqual(state.total_on_position(), 2)

    def test_loss_decrements_and_logs(self):
        state = FleetState()
        state.record_launch("ШМЕЛЬ")
        state.record_loss("ШМЕЛЬ", "Втрата дрона", "втрата дрон")
        self.assertEqual(state.group("ШМЕЛЬ").on_position, 0)
        self.assertEqual(state.group("ШМЕЛЬ").lost_total, 1)
        self.assertEqual(len(state.losses), 1)

    def test_on_position_never_negative(self):
        state = FleetState()
        state.record_return("ШМЕЛЬ")
        self.assertEqual(state.group("ШМЕЛЬ").on_position, 0)

    def test_roundtrip_serialization(self):
        state = FleetState()
        state.record_launch("КОБРА")
        state.record_incident("КОБРА", "Обрив зв'язку", "обрив зв'язку")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            save_state(state, path)
            loaded = load_state(path)

        self.assertEqual(loaded.group("КОБРА").on_position, 1)
        self.assertEqual(len(loaded.incidents), 1)

    def test_load_missing_file_returns_empty_state(self):
        state = load_state("/tmp/this-file-does-not-exist-12345.json")
        self.assertEqual(state.total_on_position(), 0)


if __name__ == "__main__":
    unittest.main()
