import unittest

from src.sheets_client import DroneRegistry

HEADER = [
    "Марка дрону", "Серійний номер", "Розташування",
    "Облікований / Не облікований", "СТАТУС", "Дата отримання", "Додаткова примітка",
]


class _FakeCell:
    def __init__(self, value):
        self.value = value


class FakeWorksheet:
    def __init__(self, title, rows):
        self.title = title
        self._rows = [list(r) for r in rows]

    def get_all_values(self):
        return [list(r) for r in self._rows]

    def row_values(self, n):
        return list(self._rows[n - 1])

    def cell(self, row, col):
        row_data = self._rows[row - 1]
        return _FakeCell(row_data[col - 1] if col - 1 < len(row_data) else "")

    def update_cell(self, row, col, value):
        while len(self._rows[row - 1]) < col:
            self._rows[row - 1].append("")
        self._rows[row - 1][col - 1] = value


class FakeSpreadsheet:
    def __init__(self, worksheets):
        self._worksheets = {ws.title: ws for ws in worksheets}

    def worksheets(self):
        return list(self._worksheets.values())

    def worksheet(self, title):
        return self._worksheets[title]


def _registry():
    m4t = FakeWorksheet("DJI Matrice 4T", [
        HEADER,
        ["DJI Matrice 4T", "1581F7K3C264200DAFYJ", "Шмєль", "облікований", "В роботі", "01.07.2026", ""],
        ["DJI Matrice 4T", "1581F7K3C263H00DD65Z", "Шмєль", "облікований", "В роботі", "18.08.2026", "стара нотатка"],
    ])
    m4e = FakeWorksheet("DJI Matrice 4E", [
        HEADER,
        ["DJI Matrice 4E", "1581F7FVC25AC00DTHLW", "Шмєль", "облікований", "В роботі", "01.07.2026", ""],
    ])
    summary = FakeWorksheet("На позиції", [
        ["Група", "Денні борти", "Потрібен ремонт", "всього в роботі"],
        ["Шмєль", "2", "0", "4"],
    ])
    return DroneRegistry(FakeSpreadsheet([m4t, m4e, summary]))


class FindBySerialTests(unittest.TestCase):
    def test_finds_existing_serial(self):
        matches = _registry().find_by_serial("1581F7K3C264200DAFYJ")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].old_status, "В роботі")
        self.assertEqual(matches[0].worksheet.title, "DJI Matrice 4T")

    def test_case_and_whitespace_insensitive(self):
        matches = _registry().find_by_serial("  1581f7k3c264200dafyj  ")
        self.assertEqual(len(matches), 1)

    def test_unknown_serial_returns_empty(self):
        self.assertEqual(_registry().find_by_serial("0000000000000000XXXX"), [])

    def test_ignores_sheets_without_serial_column(self):
        # лист "На позиції" не має колонки "Серійний номер" — не повинен
        # ані впасти з помилкою, ані дати хибний збіг.
        matches = _registry().find_by_serial("Шмєль")
        self.assertEqual(matches, [])


class SetStatusTests(unittest.TestCase):
    def test_updates_status_cell(self):
        reg = _registry()
        matches = reg.set_status("1581F7K3C264200DAFYJ", "потрібен ремонт")
        self.assertEqual(len(matches), 1)
        row = matches[0].worksheet.get_all_values()[1]
        self.assertEqual(row[4], "потрібен ремонт")

    def test_appends_to_existing_note_instead_of_overwriting(self):
        reg = _registry()
        reg.set_status("1581F7K3C263H00DD65Z", "втрачено", note="з чату Шмель, 22.08 14:00")
        row = reg.find_by_serial("1581F7K3C263H00DD65Z")[0].worksheet.get_all_values()[2]
        self.assertIn("стара нотатка", row[6])
        self.assertIn("з чату Шмель", row[6])

    def test_writes_note_when_column_empty(self):
        reg = _registry()
        reg.set_status("1581F7FVC25AC00DTHLW", "потрібен ремонт", note="Знімаємо на ремонт")
        row = reg.find_by_serial("1581F7FVC25AC00DTHLW")[0].worksheet.get_all_values()[1]
        self.assertEqual(row[6], "Знімаємо на ремонт")

    def test_unknown_serial_returns_empty_and_does_not_raise(self):
        self.assertEqual(_registry().set_status("0000000000000000XXXX", "втрачено"), [])


class PositionSummaryTests(unittest.TestCase):
    def test_formats_group_lines(self):
        summary = _registry().read_position_summary()
        self.assertIn("Шмєль", summary)
        self.assertIn("4 у роботі", summary)


if __name__ == "__main__":
    unittest.main()
