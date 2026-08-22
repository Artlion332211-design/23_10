import unittest

from src.parser import EventType, parse_message

INVENTORY_MESSAGE = """Група "Шмель"
Matrice 4T
1581F7K3C264200DAFYJ
1581F7K3C263H00DD65Z

Matrice 4E
1581F7FVC261T00DWDCC
1581F7FVC261T00DM6PU
1581F7FVC25AC00DTHLW - (Знімаємо на ремонт)

Батарейки:
Оригінал - 9 шт
5s - 12 шт
"""

LOSS_MESSAGE = """Група: "Кобра 1"
Пілот: Сурговський А.
Втрата борта:
М4Т
1581F7K3C265S00DFSGJ
Дата 20.08.26
Час 23:45
Орієнтовні координати:
37U CP 97930 82698
Причина: Почало трусити камеру і сів на дерево

"Сімсон2" в наявності:

Matrice 4e
1581F7FVC264P00DZMKT
1581F7FVC261L00DMBB2
"""

HANDOFF_MESSAGE = """"Сімсон2" в наявності:

Matrice 4e
1581F7FVC264P00DZMKT

1581F7FVC264P00DVWTP
Передали на ремонт Коброю 1
"""


class ParseInventoryMessageTests(unittest.TestCase):
    def test_only_annotated_serial_produces_event(self):
        events = parse_message(INVENTORY_MESSAGE)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].serial, "1581F7FVC25AC00DTHLW")
        self.assertIs(events[0].event_type, EventType.REPAIR)
        self.assertEqual(events[0].group, "Шмель")

    def test_plain_listed_serials_are_ignored(self):
        events = parse_message(INVENTORY_MESSAGE)
        serials = {e.serial for e in events}
        self.assertNotIn("1581F7K3C264200DAFYJ", serials)
        self.assertNotIn("1581F7FVC261T00DWDCC", serials)


class ParseLossMessageTests(unittest.TestCase):
    def test_extracts_full_loss_report(self):
        events = parse_message(LOSS_MESSAGE)
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertIs(e.event_type, EventType.LOSS)
        self.assertEqual(e.serial, "1581F7K3C265S00DFSGJ")
        self.assertEqual(e.group, "Кобра 1")
        self.assertEqual(e.pilot, "Сурговський А.")
        self.assertEqual(e.date, "20.08.26")
        self.assertEqual(e.time, "23:45")
        self.assertEqual(e.coordinates, "37U CP 97930 82698")
        self.assertEqual(e.reason, "Почало трусити камеру і сів на дерево")

    def test_trailing_inventory_block_produces_no_events(self):
        events = parse_message(LOSS_MESSAGE)
        self.assertEqual(len(events), 1)  # лише сама втрата, не подальший перелік


class ParseHandoffMessageTests(unittest.TestCase):
    def test_bare_serial_followed_by_repair_phrase(self):
        events = parse_message(HANDOFF_MESSAGE)
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertIs(e.event_type, EventType.REPAIR)
        self.assertEqual(e.serial, "1581F7FVC264P00DVWTP")
        self.assertIn("Коброю 1", e.note)


class ClassifyNoteEdgeCasesTests(unittest.TestCase):
    def test_case_insensitive_serial(self):
        events = parse_message("1581f7fvc25ac00dthlw - (Знімаємо на ремонт)")
        self.assertEqual(events[0].serial, "1581F7FVC25AC00DTHLW")

    def test_no_event_for_message_without_status_trigger(self):
        events = parse_message("Всім привіт, як справи?")
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
