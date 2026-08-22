from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import List, Optional

from .config import Config


class EventType(enum.Enum):
    LOSS = "loss"
    INCIDENT = "incident"
    LAUNCH = "launch"
    RETURN = "return"
    OTHER = "other"


@dataclass
class ClassifiedMessage:
    event_type: EventType
    matched_keyword: Optional[str]
    text: str


def _find_keyword(text_lower: str, keywords: List[str]) -> Optional[str]:
    for kw in keywords:
        if kw in text_lower:
            return kw
    return None


def classify(text: str, cfg: Config) -> ClassifiedMessage:
    text_lower = text.lower()

    # Нештатні ситуації перевіряємо перед втратами: інакше "втрата зв'язку"
    # (тимчасовий обрив, не втрата борту) зчитувалась би як LOSS.
    kw = _find_keyword(text_lower, cfg.incident_keywords)
    if kw:
        return ClassifiedMessage(EventType.INCIDENT, kw, text)

    kw = _find_keyword(text_lower, cfg.loss_keywords)
    if kw:
        return ClassifiedMessage(EventType.LOSS, kw, text)

    kw = _find_keyword(text_lower, cfg.launch_keywords)
    if kw:
        return ClassifiedMessage(EventType.LAUNCH, kw, text)

    kw = _find_keyword(text_lower, cfg.return_keywords)
    if kw:
        return ClassifiedMessage(EventType.RETURN, kw, text)

    return ClassifiedMessage(EventType.OTHER, None, text)
