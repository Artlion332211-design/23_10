from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SERIAL_COLUMN = "Серійний номер"
STATUS_COLUMN = "СТАТУС"
NOTE_COLUMN = "Додаткова примітка"


@dataclass
class SheetMatch:
    worksheet: "gspread.Worksheet"
    row: int  # 1-based, з урахуванням заголовка
    old_status: Optional[str]


class DroneRegistry:
    """Пошук/оновлення борта в реєстрі "РР-БпЛА" за серійним номером.

    Торкається лише клітинок СТАТУС і "Додаткова примітка" в рядку, що вже
    існує — ніколи не додає й не видаляє рядки/колонки, щоб не зламати
    формули на інших листах книги (напр. зведення "На позиції").
    """

    def __init__(self, spreadsheet, model_sheets: Optional[List[str]] = None):
        self._spreadsheet = spreadsheet
        self._model_sheets = model_sheets

    def _candidate_worksheets(self):
        if self._model_sheets:
            return [self._spreadsheet.worksheet(name) for name in self._model_sheets]
        return self._spreadsheet.worksheets()

    def list_worksheet_titles(self) -> List[str]:
        return [ws.title for ws in self._candidate_worksheets()]

    def find_by_serial(self, serial: str) -> List[SheetMatch]:
        serial_norm = serial.strip().upper()
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
                if len(row) > serial_idx and row[serial_idx].strip().upper() == serial_norm:
                    old_status = row[status_idx] if len(row) > status_idx else None
                    matches.append(SheetMatch(worksheet=ws, row=row_num, old_status=old_status))
        return matches

    def set_status(self, serial: str, new_status: str, note: Optional[str] = None) -> List[SheetMatch]:
        matches = self.find_by_serial(serial)
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
                serial, match.old_status, new_status, match.worksheet.title, match.row,
            )
        if not matches:
            logger.warning("Серійник %s не знайдено в реєстрі — статус не змінено", serial)
        return matches

    def read_position_summary(self, sheet_name: str = "На позиції") -> str:
        ws = self._spreadsheet.worksheet(sheet_name)
        values = ws.get_all_values()
        if not values:
            return ""
        header = [h.strip() for h in values[0]]
        group_idx = _find_column(header, ["Груп", "Позиція"])
        total_idx = _find_column(header, ["всього в роботі"])
        repair_idx = _find_column(header, ["потрібен ремонт"])

        lines = []
        for row in values[1:]:
            if group_idx is None or group_idx >= len(row) or not row[group_idx].strip():
                continue
            group = row[group_idx].strip()
            parts = []
            if total_idx is not None and total_idx < len(row) and row[total_idx].strip():
                parts.append(f"{row[total_idx].strip()} у роботі")
            if repair_idx is not None and repair_idx < len(row) and row[repair_idx].strip() not in ("", "0"):
                parts.append(f"{row[repair_idx].strip()} на ремонті")
            lines.append(f"  {group}: {', '.join(parts) if parts else '—'}")
        return "\n".join(lines)


def _find_column(header: List[str], name_variants: List[str]) -> Optional[int]:
    for idx, title in enumerate(header):
        title_lower = title.strip().lower()
        if any(variant.lower() in title_lower for variant in name_variants):
            return idx
    return None


def open_registry(spreadsheet_id: str, credentials_path: str, model_sheets: Optional[List[str]] = None) -> DroneRegistry:
    creds = Credentials.from_service_account_file(credentials_path, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(spreadsheet_id)
    return DroneRegistry(spreadsheet, model_sheets=model_sheets)
