import unittest

from src.sheets_client import AmbiguousSerialError, DroneRegistry

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

    def append_row(self, values, value_input_option=None):
        self._rows.append(list(values))


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
        ["Група", "Денні борти", "Нічні борти", "Потрібен ремонт", "всього в роботі"],
        ["Шмєль", "2", "2", "0", "4"],
        ["Кобра", "2", "3", "0", "5"],
    ])
    lost = FakeWorksheet("ВТРАЧЕНО", [HEADER])
    return DroneRegistry(FakeSpreadsheet([m4t, m4e, summary, lost]))


class FindBySerialTests(unittest.TestCase):
    def test_finds_existing_serial(self):
        matches = _registry().find_by_serial("1581F7K3C264200DAFYJ")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].old_status, "В роботі")
        self.assertEqual(matches[0].worksheet.title, "DJI Matrice 4T")

    def test_matches_by_last_5_characters_only(self):
        # Пілот міг передати лише кінцівку серійника, не весь 20-символьний рядок.
        matches = _registry().find_by_serial("DAFYJ")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].full_serial, "1581F7K3C264200DAFYJ")

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

    def test_ambiguous_suffix_returns_all_candidates_without_raising(self):
        # find_by_serial сам по собі лише читає — рішення, що робити з
        # неоднозначністю, приймає викликач (set_status).
        m4t = FakeWorksheet("DJI Matrice 4T", [HEADER, ["DJI Matrice 4T", "AAAAAAAAAAAAAAAAAXXXX", "Шмєль", "облікований", "В роботі", "", ""]])
        reg = DroneRegistry(FakeSpreadsheet([m4t]))
        matches = reg.find_by_serial("AXXXX")
        self.assertEqual({m.full_serial for m in matches}, {"AAAAAAAAAAAAAAAAAXXXX"})

    def test_two_different_serials_sharing_last_5_chars(self):
        m4t = FakeWorksheet("DJI Matrice 4T", [HEADER, ["DJI Matrice 4T", "1111111111111112ZZZZ", "Шмєль", "облікований", "В роботі", "", ""]])
        m4e = FakeWorksheet("DJI Matrice 4E", [HEADER, ["DJI Matrice 4E", "2222222222222222ZZZZ", "Кобра", "облікований", "В роботі", "", ""]])
        reg = DroneRegistry(FakeSpreadsheet([m4t, m4e]))
        matches = reg.find_by_serial("2ZZZZ")
        self.assertEqual({m.full_serial for m in matches}, {"1111111111111112ZZZZ", "2222222222222222ZZZZ"})


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
        self.assertIn("Знімаємо на ремонт", row[6])
        self.assertTrue(row[6].startswith("["))  # датовий штамп зміни статусу

    def test_skips_write_when_status_unchanged(self):
        reg = _registry()
        matches = reg.set_status("1581F7K3C264200DAFYJ", "В роботі")  # вже "В роботі"
        self.assertEqual(len(matches), 1)
        row = matches[0].worksheet.get_all_values()[1]
        self.assertEqual(row[4], "В роботі")
        self.assertEqual(row[6], "")  # примітку теж не чіпали

    def test_unknown_serial_returns_empty_and_does_not_raise(self):
        self.assertEqual(_registry().set_status("0000000000000000XXXX", "втрачено"), [])

    def test_ambiguous_suffix_raises_and_touches_neither_row(self):
        m4t = FakeWorksheet("DJI Matrice 4T", [HEADER, ["DJI Matrice 4T", "1111111111111112ZZZZ", "Шмєль", "облікований", "В роботі", "", ""]])
        m4e = FakeWorksheet("DJI Matrice 4E", [HEADER, ["DJI Matrice 4E", "2222222222222222ZZZZ", "Кобра", "облікований", "В роботі", "", ""]])
        reg = DroneRegistry(FakeSpreadsheet([m4t, m4e]))

        with self.assertRaises(AmbiguousSerialError) as ctx:
            reg.set_status("2ZZZZ", "потрібен ремонт")
        self.assertEqual(set(ctx.exception.candidates), {"1111111111111112ZZZZ", "2222222222222222ZZZZ"})

        self.assertEqual(m4t.get_all_values()[1][4], "В роботі")
        self.assertEqual(m4e.get_all_values()[1][4], "В роботі")


class PositionStatsTests(unittest.TestCase):
    def test_reads_day_night_repair_per_group(self):
        stats = _registry().read_position_stats(["Кобра"])
        self.assertEqual(len(stats), 1)
        self.assertTrue(stats[0].found)
        self.assertEqual(stats[0].day_drones, 2)
        self.assertEqual(stats[0].night_drones, 3)
        self.assertEqual(stats[0].total_in_service, 5)

    def test_normalizes_ie_ye_spelling_mismatch(self):
        # У живих даних лист має "Шмєль", а користувачі часто пишуть "Шмель".
        stats = _registry().read_position_stats(["Шмель"])
        self.assertEqual(len(stats), 1)
        self.assertTrue(stats[0].found)
        self.assertEqual(stats[0].day_drones, 2)

    def test_missing_group_marked_not_found(self):
        stats = _registry().read_position_stats(["Вій"])
        self.assertEqual(len(stats), 1)
        self.assertFalse(stats[0].found)

    def test_preserves_requested_order(self):
        stats = _registry().read_position_stats(["Кобра", "Шмель"])
        self.assertEqual([s.group for s in stats], ["Кобра", "Шмель"])


class AppendLossRecordTests(unittest.TestCase):
    def test_appends_row_matched_by_header(self):
        reg = _registry()
        reg.append_loss_record(
            "ВТРАЧЕНО", model="М4Т", serial="1581F7K3C265S00DFSGJ", group="Кобра 1",
            status="втрачено", note="Пілот: Сурговський А.; Причина: сів на дерево",
        )
        rows = reg._spreadsheet.worksheet("ВТРАЧЕНО").get_all_values()
        self.assertEqual(len(rows), 2)
        new_row = rows[1]
        self.assertEqual(new_row[0], "М4Т")
        self.assertEqual(new_row[1], "1581F7K3C265S00DFSGJ")
        self.assertEqual(new_row[2], "Кобра 1")
        self.assertEqual(new_row[4], "втрачено")
        self.assertIn("Сурговський", new_row[6])

    def test_falls_back_to_observed_column_order_when_header_blank(self):
        lost_blank_header = FakeWorksheet("ВТРАЧЕНО", [["", "", "", "", "", "", ""]])
        reg = DroneRegistry(FakeSpreadsheet([lost_blank_header]))
        reg.append_loss_record(
            "ВТРАЧЕНО", model="М4Т", serial="XYZ", group="Кобра", status="втрачено", note="деталі",
        )
        row = lost_blank_header.get_all_values()[1]
        self.assertEqual(row[0], "М4Т")
        self.assertEqual(row[1], "XYZ")
        self.assertEqual(row[4], "втрачено")


class AddNewRowTests(unittest.TestCase):
    def test_adds_row_to_matching_model_sheet_by_cyrillic_abbreviation(self):
        reg = _registry()
        created = reg.add_new_row("М4Т", "9999999999999999NEWW", "Шмель", "В роботі", None)
        self.assertTrue(created)
        rows = reg._spreadsheet.worksheet("DJI Matrice 4T").get_all_values()
        self.assertEqual(rows[-1][1], "9999999999999999NEWW")
        self.assertEqual(rows[-1][2], "Шмель")
        self.assertEqual(rows[-1][4], "В роботі")

    def test_returns_false_without_recognized_model(self):
        reg = _registry()
        self.assertFalse(reg.add_new_row(None, "9999999999999999NEWW", "Шмель", "В роботі", None))

    def test_returns_false_for_unknown_model(self):
        reg = _registry()
        self.assertFalse(reg.add_new_row("Autel EVO", "9999999999999999NEWW", "Шмель", "В роботі", None))


class ListNotInServiceTests(unittest.TestCase):
    def test_excludes_active_status_and_blank_rows(self):
        reg = _registry()
        reg.set_status("1581F7K3C263H00DD65Z", "потрібен ремонт")
        not_in_service = reg.list_not_in_service(active_status="В роботі")
        serials = {row["serial"] for row in not_in_service}
        self.assertIn("1581F7K3C263H00DD65Z", serials)
        self.assertNotIn("1581F7K3C264200DAFYJ", serials)  # лишається "В роботі"


if __name__ == "__main__":
    unittest.main()
