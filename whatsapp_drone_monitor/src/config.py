from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Dict, List, Optional, Union

import yaml


@dataclasses.dataclass
class Config:
    chats: Dict[str, str]  # {"назва чату у WhatsApp": "позначення групи у звітах"}
    admin_chat: str  # чат, куди бот шле алерти й звіти (напр. "Ви")

    spreadsheet_id: str  # ID таблиці "РР-БпЛА" (з URL між /d/ і /edit)
    credentials_path: str = "./credentials.json"  # ключ сервісного акаунта Google
    # Якщо задано — шукаємо серійники лише в цих листах книги. Якщо None —
    # перевіряємо всі листи (ті, де немає колонки "Серійний номер", просто
    # пропускаються), що стійкіше до нових листів моделей у майбутньому.
    model_sheets: Optional[List[str]] = None
    position_summary_sheet: str = "На позиції"

    # Мають ЗБІГАТИСЯ ДОСЛІВНО з варіантами випадаючого списку в колонці
    # СТАТУС — інакше запис або підсвітиться як помилка валідації, або
    # просто не буде відповідати іншим рядкам з тим самим сенсом.
    status_repair: str = "потрібен ремонт"
    status_lost: str = "втрачено"

    poll_interval_seconds: int = 20
    report_interval_hours: int = 6
    chrome_profile_dir: str = "./chrome_profile"
    state_file: str = "./state.json"
    log_file: str = "./monitor.log"

    @classmethod
    def load(cls, path: Union[str, Path]) -> "Config":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"Невідомі поля в конфігу: {sorted(unknown)}")
        return cls(**data)
