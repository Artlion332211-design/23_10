import unittest

from src.config import Config
from src.parser import EventType, classify


def _cfg() -> Config:
    return Config(chats={}, admin_chat="admin")


class ClassifyTests(unittest.TestCase):
    def test_loss_detected(self):
        result = classify("Групо, увага! Втрата дрона FPV на координатах 49.1,36.2", _cfg())
        self.assertIs(result.event_type, EventType.LOSS)

    def test_connection_incident_not_confused_with_loss(self):
        result = classify("Втрата зв'язку з бортом, чекаємо відновлення", _cfg())
        self.assertIs(result.event_type, EventType.INCIDENT)

    def test_launch_detected(self):
        result = classify("Борт заступив на позицію", _cfg())
        self.assertIs(result.event_type, EventType.LAUNCH)

    def test_return_detected(self):
        result = classify("Екіпаж повернувся, посадка виконана", _cfg())
        self.assertIs(result.event_type, EventType.RETURN)

    def test_other_for_unrelated_text(self):
        result = classify("Прийняв, дякую", _cfg())
        self.assertIs(result.event_type, EventType.OTHER)

    def test_case_insensitive(self):
        result = classify("ВТРАТА ДРОНА через ворожий РЕБ", _cfg())
        self.assertIs(result.event_type, EventType.LOSS)


if __name__ == "__main__":
    unittest.main()
