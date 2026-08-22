from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Dict, List, Optional

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SERIAL_COLUMN = "Серійний номер"
STATUS_COLUMN = "СТАТУС"
NOTE_COLUMN = "Додаткова примітка"

# Порядок колонок, реально спостережений у наявних рядках листа "ВТРАЧЕНО" —
# запасний варіант на випадок, якщо заголовок листа не вдасться розпізнати
# за назвою (напр. порожній чи змінений рядок заголовка).
FALLBACK_LOSS_COLUMNS = [
    "Марка дрону", "Серійний номер", "Розташування",
    "Облікований / Не облікований", "СТАТУС", "Дата отримання", "Додаткова примітка",
]


@dataclass
class SheetMatch:
    worksheet: "gspread.Worksheet"
    row: int  # 1-based, з урахуванням заголовка
    old_status: Optional[str]
    full_serial: str


class AmbiguousSerialError(Exception):
    """Останні символи серійника збігаються з кількома різними бортами."""

    def __init__(self, serial: str, candidates: List[str]):
        self.serial = serial
        self.candidates = candidates
        super().__init__(f"Серійник {serial}: кілька різних бортів з таким закінченням: {candidates}")


@dataclass
class PositionStat:
    group: str
    day_drones: int = 0
    night_drones: int = 0
    repair_count: int = 0
    total_in_service: int = 0
    found: bool = True


class DroneRegistry:
    """Пошук/оновлення борта в реєстрі "РР-БпЛА" за серійним номером.

    Торкається лише клітинок СТАТУС і "Додаткова примітка" в рядку, що вже
    існує — ніколи не додає й не видаляє рядки/колонки, щоб не зламати
    формули на інших листах книги (напр. зведення "На позиції").
    """

    def __init__(self, spreadsheet, model_sheets: Optional[List[str]] = None, serial_suffix_length: int = 5):
        self._spreadsheet = spreadsheet
        self._model_sheets = model_sheets
        self._suffix_length = serial_suffix_length

    def _candidate_worksheets(self):
        if self._model_sheets:
            return [self._spreadsheet.worksheet(name) for name in self._model_sheets]
        return self._spreadsheet.worksheets()

    def list_worksheet_titles(self) -> List[str]:
        return [ws.title for ws in self._candidate_worksheets()]

    def read_header(self, sheet_name: str) -> List[str]:
        return [h.strip() for h in self._spreadsheet.worksheet(sheet_name).row_values(1)]

    def find_by_serial(self, serial: str) -> List[SheetMatch]:
        """Шукає борт за останніми `serial_suffix_length` символами серійника.

        Пілоти в чаті не завжди передають повний 20-символьний серійник без
        помилок — кінцівка помітніша й надійніша (часто саме її звіряють
        вручну на наклейці борта), тому звіряємо саме її, а не рядок
        цілком.
        """
        query_suffix = _serial_suffix(serial, self._suffix_length)
        matches: List[SheetMatch] = []
        for ws in self._candidate_worksheets():
            values = ws.get_all_values()
            if not values:
                continue
            header = values[0]
            if SERIAL_COLUMN not in header or STATUS_COLUMN not in header:
                continue  # не таблиця бортів (напр. "Звіт", "На позиції")
            serial_idx = header.index(SERIAL_COLUMN)
            status_idx = header.index(STATUS_COLUMN)
            for row_num, row in enumerate(values[1:], start=2):
                if serial_idx >= len(row):
                    continue
                cell_serial = row[serial_idx].strip()
                if cell_serial and _serial_suffix(cell_serial, self._suffix_length) == query_suffix:
                    old_status = row[status_idx] if len(row) > status_idx else None
                    matches.append(
                        SheetMatch(worksheet=ws, row=row_num, old_status=old_status, full_serial=cell_serial)
                    )
        return matches

    def set_status(self, serial: str, new_status: str, note: Optional[str] = None) -> List[SheetMatch]:
        matches = self.find_by_serial(serial)

        distinct_serials = {m.full_serial for m in matches}
        if len(distinct_serials) > 1:
            # Збіг лише за останніми символами вказує на КІЛЬКА різних
            # реальних бортів — статус нікому з них не міняємо, інакше
            # ризикуємо тихо підмінити чужий запис.
            raise AmbiguousSerialError(serial, sorted(distinct_serials))

        for match in matches:
            header = match.worksheet.row_values(1)
            status_idx = header.index(STATUS_COLUMN) + 1  # gspread рахує колонки з 1
            match.worksheet.update_cell(match.row, status_idx, new_status)

            if note and NOTE_COLUMN in header:
                note_idx = header.index(NOTE_COLUMN) + 1
                existing = (match.worksheet.cell(match.row, note_idx).value or "").strip()
                combined = f"{existing}; {note}" if existing else note
                match.worksheet.update_cell(match.row, note_idx, combined)

            logger.info(
                "Статус %s: %s -> %s (лист '%s', рядок %s)",
                match.full_serial, match.old_status, new_status, match.worksheet.title, match.row,
            )
        if not matches:
            logger.warning("Серійник %s не знайдено в реєстрі — статус не змінено", serial)
        return matches

    def read_position_stats(
        self, groups: Optional[List[str]] = None, sheet_name: str = "На позиції"
    ) -> List[PositionStat]:
        """Читає денні/нічні борти й "потрібен ремонт" з листа зведення.

        Значення шукаються за НАЗВОЮ заголовка, а не позицією колонки — сам
        лист це щоденний людський звіт, і порядок/набір колонок моделей у
        ньому вже не раз змінювався.
        """
        ws = self._spreadsheet.worksheet(sheet_name)
        values = ws.get_all_values()
        if not values:
            return [PositionStat(group=g, found=False) for g in (groups or [])]

        header = [h.strip() for h in values[0]]
        group_idx = _find_column(header, ["груп", "позиці"])
        day_idx = _find_column(header, ["денні борти"])
        night_idx = _find_column(header, ["нічні борти"])
        repair_idx = _find_column(header, ["потрібен ремонт"])
        total_idx = _find_column(header, ["всього в роботі"])

        by_group: Dict[str, PositionStat] = {}
        for row in values[1:]:
            if group_idx is None or group_idx >= len(row) or not row[group_idx].strip():
                continue
            name = row[group_idx].strip()
            stat = PositionStat(
                group=name,
                day_drones=_cell_int(row, day_idx),
                night_drones=_cell_int(row, night_idx),
                repair_count=_cell_int(row, repair_idx),
                total_in_service=_cell_int(row, total_idx),
            )
            by_group[_normalize_group_name(name)] = stat

        if groups is None:
            return list(by_group.values())

        result = []
        for wanted in groups:
            stat = by_group.get(_normalize_group_name(wanted))
            # Показуємо назву групи так, як її задали в конфігу (напр.
            # "Шмель"), а не як вона записана на листі (там "Шмєль") —
            # звіт має виглядати передбачувано незалежно від правопису в
            # таблиці.
            result.append(replace(stat, group=wanted) if stat else PositionStat(group=wanted, found=False))
        return result

    def append_loss_record(
        self, sheet_name: str, *, model: Optional[str], serial: str, group: Optional[str],
        status: str, note: Optional[str],
    ) -> None:
        """Додає рядок у журнал втрат (лист "ВТРАЧЕНО").

        На відміну від set_status, тут ЗАВЖДИ додається новий рядок, а не
        шукається існуючий — так само, як це зараз роблять люди: живі дані
        показують, що втрачений борт часто взагалі відсутній у листах
        моделей (це окремий журнал, а не статус активного борта).
        """
        ws = self._spreadsheet.worksheet(sheet_name)
        header = [h.strip() for h in ws.row_values(1)]
        values = {
            "Марка дрону": model or "",
            "Серійний номер": serial,
            "Розташування": group or "",
            "СТАТУС": status,
            "Дата отримання": datetime.now().strftime("%d.%m.%Y"),
            "Додаткова примітка": note or "",
        }
        if SERIAL_COLUMN in header:
            row = [""] * len(header)
            for key, value in values.items():
                if key in header:
                    row[header.index(key)] = value
        else:
            row = [values.get(col, "") for col in FALLBACK_LOSS_COLUMNS]

        ws.append_row(row, value_input_option="USER_ENTERED")
        logger.info("Додано запис про втрату %s у лист '%s'", serial, sheet_name)


def _find_column(header: List[str], name_variants: List[str]) -> Optional[int]:
    for idx, title in enumerate(header):
        title_lower = title.strip().lower()
        if any(variant.lower() in title_lower for variant in name_variants):
            return idx
    return None


def _cell_int(row: List[str], idx: Optional[int]) -> int:
    if idx is None or idx >= len(row):
        return 0
    match = re.match(r"\d+", row[idx].strip())
    return int(match.group()) if match else 0


def _normalize_group_name(name: str) -> str:
    # У живих даних трапляється і "Шмель", і "Шмєль" — усуваємо цю саме
    # літерну розбіжність, а не будуємо загальний нечіткий пошук.
    return name.strip().lower().replace("є", "е")


def _serial_suffix(serial: str, length: int) -> str:
    return serial.strip().upper()[-length:]


def open_registry(
    spreadsheet_id: str,
    credentials_path: str,
    model_sheets: Optional[List[str]] = None,
    serial_suffix_length: int = 5,
) -> DroneRegistry:
    creds = Credentials.from_service_account_file(credentials_path, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(spreadsheet_id)
    return DroneRegistry(spreadsheet, model_sheets=model_sheets, serial_suffix_length=serial_suffix_length)
