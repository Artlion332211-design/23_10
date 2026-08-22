from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Dict, List, Union

import yaml

# LOSS-фрази навмисно прив'язані до "дрон"/"борт", а не до голого кореня
# "втрат", інакше "втрата зв'язку" (це нештатна ситуація) класифікувалась би
# як втрата дрона.
DEFAULT_LOSS_KEYWORDS = [
    "втрата дрон", "втратили дрон", "втрачений дрон", "втрачено дрон",
    "дрон втрачен", "борт втрачен", "втрачений борт", "втратили борт",
    "збили дрон", "збит дрон", "підбили дрон", "підбит дрон",
    "не повернувся", "не повернувс",
]
DEFAULT_INCIDENT_KEYWORDS = [
    "нештатн", "аварі", "поломк", "несправ",
    "обрив зв'язку", "втрата зв'язку", "втратили зв'язок", "пропав зв'язок",
    "пожежа", "поранен", "травм", "збій",
]
DEFAULT_LAUNCH_KEYWORDS = [
    "виліт", "вилетів", "вилетіли", "зайняв позицію", "заступив на позицію", "на позиції",
]
DEFAULT_RETURN_KEYWORDS = [
    "повернувся", "повернення", "посадка", "приземлився", "приземлились",
    "зняв з позиції", "залишив позицію", "знятий з позиції",
]


@dataclasses.dataclass
class Config:
    chats: Dict[str, str]  # {"назва чату у WhatsApp": "позначення групи у звітах"}
    admin_chat: str  # чат, куди бот шле алерти й звіти (напр. "Повідомлення собі")
    poll_interval_seconds: int = 20
    report_interval_hours: int = 6
    chrome_profile_dir: str = "./chrome_profile"
    state_file: str = "./state.json"
    log_file: str = "./monitor.log"
    loss_keywords: List[str] = dataclasses.field(default_factory=lambda: list(DEFAULT_LOSS_KEYWORDS))
    incident_keywords: List[str] = dataclasses.field(default_factory=lambda: list(DEFAULT_INCIDENT_KEYWORDS))
    launch_keywords: List[str] = dataclasses.field(default_factory=lambda: list(DEFAULT_LAUNCH_KEYWORDS))
    return_keywords: List[str] = dataclasses.field(default_factory=lambda: list(DEFAULT_RETURN_KEYWORDS))

    @classmethod
    def load(cls, path: Union[str, Path]) -> "Config":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"Невідомі поля в конфігу: {sorted(unknown)}")
        return cls(**data)
