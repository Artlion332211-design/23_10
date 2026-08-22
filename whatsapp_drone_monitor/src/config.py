from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import List, Optional, Union

import yaml


@dataclasses.dataclass
class Config:
    chats: List[str]  # назви WhatsApp-чатів пілотів, які моніторимо
    admin_chat: str  # чат/контакт, куди бот шле алерти й звіти

    spreadsheet_id: str  # ID таблиці "РР-БпЛА" (з URL між /d/ і /edit)
    credentials_path: str = "./credentials.json"  # ключ сервісного акаунта Google
    # Якщо задано — шукаємо серійники лише в цих листах книги. Якщо None —
    # перевіряємо всі листи (ті, де немає колонки "Серійний номер", просто
    # пропускаються), що стійкіше до нових листів моделей у майбутньому.
    model_sheets: Optional[List[str]] = None
    position_summary_sheet: str = "На позиції"
    loss_log_sheet: str = "ВТРАЧЕНО"
    # Борт шукаємо за останніми N символами серійника (кінцівку легше
    # звірити вручну, ніж увесь 20-символьний рядок без помилок).
    serial_suffix_length: int = 5

    # Позиції для денного звіту, у тому порядку, в якому їх треба показувати.
    daily_report_groups: List[str] = dataclasses.field(
        default_factory=lambda: ["Сімсон", "Шмель", "Кобра", "Вій"]
    )
    daily_report_time: str = "21:00"  # HH:MM, локальний час машини, де працює бот

    # Мають ЗБІГАТИСЯ ДОСЛІВНО з варіантами випадаючого списку в колонці
    # СТАТУС — інакше запис або підсвітиться як помилка валідації, або
    # просто не буде відповідати іншим рядкам з тим самим сенсом.
    status_repair: str = "потрібен ремонт"
    status_lost: str = "втрачено"
    status_active: str = "В роботі"

    poll_interval_seconds: int = 20
    chrome_profile_dir: str = "./chrome_profile"
    state_file: str = "./state.json"
    log_file: str = "./monitor.log"

    def __post_init__(self) -> None:
        hours, _, minutes = self.daily_report_time.partition(":")
        valid = hours.isdigit() and minutes.isdigit() and 0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59
        if not valid:
            raise ValueError(f"daily_report_time має бути у форматі HH:MM, отримано: {self.daily_report_time!r}")

    @classmethod
    def load(cls, path: Union[str, Path]) -> "Config":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"Невідомі поля в конфігу: {sorted(unknown)}")
        return cls(**data)
