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
    def test_annotated_serial_is_repair(self):
        events = parse_message(INVENTORY_MESSAGE)
        by_serial = {e.serial: e for e in events}
        target = by_serial["1581F7FVC25AC00DTHLW"]
        self.assertIs(target.event_type, EventType.REPAIR)
        self.assertEqual(target.group, "Шмель")
        self.assertEqual(target.model, "Matrice 4E")

    def test_plain_listed_serials_become_active(self):
        events = parse_message(INVENTORY_MESSAGE)
        by_serial = {e.serial: e for e in events}
        for serial in ["1581F7K3C264200DAFYJ", "1581F7K3C263H00DD65Z", "1581F7FVC261T00DWDCC", "1581F7FVC261T00DM6PU"]:
            self.assertIs(by_serial[serial].event_type, EventType.ACTIVE, serial)
            self.assertEqual(by_serial[serial].group, "Шмель")
        self.assertEqual(by_serial["1581F7K3C264200DAFYJ"].model, "Matrice 4T")
        self.assertEqual(by_serial["1581F7FVC261T00DM6PU"].model, "Matrice 4E")

    def test_total_event_count_matches_all_serials_in_message(self):
        events = parse_message(INVENTORY_MESSAGE)
        self.assertEqual(len(events), 5)  # 4 в роботі + 1 ремонт, батарейки/пропи не рахуються


class ParseLossMessageTests(unittest.TestCase):
    def test_extracts_full_loss_report(self):
        events = parse_message(LOSS_MESSAGE)
        losses = [e for e in events if e.event_type is EventType.LOSS]
        self.assertEqual(len(losses), 1)
        e = losses[0]
        self.assertEqual(e.serial, "1581F7K3C265S00DFSGJ")
        self.assertEqual(e.group, "Кобра 1")
        self.assertEqual(e.pilot, "Сурговський А.")
        self.assertEqual(e.date, "20.08.26")
        self.assertEqual(e.time, "23:45")
        self.assertEqual(e.coordinates, "37U CP 97930 82698")
        self.assertEqual(e.reason, "Почало трусити камеру і сів на дерево")

    def test_trailing_inventory_becomes_active_not_loss(self):
        events = parse_message(LOSS_MESSAGE)
        trailing = [e for e in events if e.serial in ("1581F7FVC264P00DZMKT", "1581F7FVC261L00DMBB2")]
        self.assertEqual(len(trailing), 2)
        for e in trailing:
            self.assertIs(e.event_type, EventType.ACTIVE)
            self.assertEqual(e.group, "Сімсон2")


class ParseHandoffMessageTests(unittest.TestCase):
    def test_bare_serial_followed_by_repair_phrase(self):
        events = parse_message(HANDOFF_MESSAGE)
        by_serial = {e.serial: e for e in events}
        target = by_serial["1581F7FVC264P00DVWTP"]
        self.assertIs(target.event_type, EventType.REPAIR)
        self.assertIn("Коброю 1", target.note)
        # Сусідній серійник у тому ж переліку не повинен стати "ремонтом".
        self.assertIs(by_serial["1581F7FVC264P00DZMKT"].event_type, EventType.ACTIVE)


class ClassifyNoteEdgeCasesTests(unittest.TestCase):
    def test_case_insensitive_serial(self):
        events = parse_message("1581f7fvc25ac00dthlw - (Знімаємо на ремонт)")
        self.assertEqual(events[0].serial, "1581F7FVC25AC00DTHLW")

    def test_no_event_for_message_without_serial(self):
        events = parse_message("Всім привіт, як справи?")
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
