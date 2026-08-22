from __future__ import annotations

from datetime import datetime, timedelta
from typing import List

from .state import ProcessedEvent

RECENT_WINDOW_HOURS = 24

EVENT_LABELS = {
    "repair": "🔧 На ремонт",
    "loss": "🚨 Втрата",
    "not_found": "❓ Не знайдено в реєстрі",
}


def format_event_alert(event: ProcessedEvent) -> str:
    label = EVENT_LABELS.get(event.event_type, event.event_type)
    lines = [f"{label} — серійник {event.serial}"]
    if event.group:
        lines.append(f"Група: {event.group}")
    if event.event_type == "not_found":
        lines.append("Серійника немає в таблиці «РР-БпЛА» — потрібна ручна перевірка.")
    else:
        lines.append(f"Статус у таблиці: {event.old_status or '—'} → {event.new_status}")
        lines.append(f"Лист: {event.sheet}")
    if event.note:
        lines.append(f"Деталі: {_truncate(event.note, 300)}")
    return "\n".join(lines)


def format_status_report(position_summary: str, recent_events: List[ProcessedEvent]) -> str:
    now = datetime.now()
    lines = [f"📊 Звіт по БпЛА станом на {now.strftime('%H:%M %d.%m.%Y')}", ""]
    lines.append("На позиції (за даними таблиці «РР-БпЛА»):")
    lines.append(position_summary if position_summary else "  (не вдалося прочитати лист «На позиції»)")

    recent = _recent(recent_events)
    lines.append("")
    lines.append(f"Зміни статусів за останні {RECENT_WINDOW_HOURS} год: {len(recent)}")
    for e in recent:
        label = EVENT_LABELS.get(e.event_type, e.event_type)
        group = f"[{e.group}] " if e.group else ""
        lines.append(f"  {label} {group}{_short_time(e.time)} — {e.serial}")

    return "\n".join(lines)


def _recent(entries: List[ProcessedEvent], hours: int = RECENT_WINDOW_HOURS) -> List[ProcessedEvent]:
    cutoff = datetime.now() - timedelta(hours=hours)
    return [e for e in entries if datetime.fromisoformat(e.time) >= cutoff]


def _short_time(iso_time: str) -> str:
    return datetime.fromisoformat(iso_time).strftime("%H:%M")


def _truncate(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
