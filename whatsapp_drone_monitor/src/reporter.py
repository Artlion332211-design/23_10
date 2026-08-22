from __future__ import annotations

from datetime import datetime, timedelta
from typing import List

from .sheets_client import PositionStat
from .state import ProcessedEvent

RECENT_WINDOW_HOURS = 24

EVENT_LABELS = {
    "repair": "🔧 На ремонт",
    "loss": "🚨 Втрата",
    "not_found": "❓ Не знайдено в реєстрі",
    "ambiguous": "⚠️ Кілька збігів за серійником",
}


def format_event_alert(event: ProcessedEvent) -> str:
    label = EVENT_LABELS.get(event.event_type, event.event_type)
    lines = [f"{label} — серійник {event.serial}"]
    if event.group:
        lines.append(f"Група: {event.group}")
    if event.event_type == "not_found":
        lines.append("Серійника немає в таблиці «РР-БпЛА» — потрібна ручна перевірка.")
    elif event.event_type == "ambiguous":
        lines.append("Останні символи збігаються з кількома різними бортами — статус НЕ змінено.")
    else:
        lines.append(f"Статус у таблиці: {event.old_status or '—'} → {event.new_status}")
        lines.append(f"Лист: {event.sheet}")
    if event.note:
        lines.append(f"Деталі: {_truncate(event.note, 300)}")
    return "\n".join(lines)


def format_position_stats(stats: List[PositionStat]) -> str:
    lines = []
    total_day = total_night = total_repair = 0
    for s in stats:
        if not s.found:
            lines.append(f"  {s.group}: не знайдено на листі «На позиції»")
            continue
        lines.append(
            f"  {s.group}: {s.day_drones} денних + {s.night_drones} нічних на позиції, "
            f"{s.repair_count} у ремонті"
        )
        total_day += s.day_drones
        total_night += s.night_drones
        total_repair += s.repair_count
    lines.append(f"  Разом: {total_day} денних, {total_night} нічних, {total_repair} у ремонті")
    return "\n".join(lines)


def format_daily_report(stats: List[PositionStat], events: List[ProcessedEvent]) -> str:
    now = datetime.now()
    recent = _recent(events)
    losses = [e for e in recent if e.event_type == "loss"]
    repairs = [e for e in recent if e.event_type == "repair"]

    lines = [f"📊 Денний звіт станом на {now.strftime('%H:%M %d.%m.%Y')}", "", "Позиції:"]
    lines.append(format_position_stats(stats))
    lines.append("")
    lines.append(f"За останні {RECENT_WINDOW_HOURS} год: втрат — {len(losses)}, передано на ремонт — {len(repairs)}")
    for e in losses:
        group = f"[{e.group}] " if e.group else ""
        lines.append(f"  🚨 Втрата {group}{_short_time(e.time)} — {e.serial}")
    for e in repairs:
        group = f"[{e.group}] " if e.group else ""
        lines.append(f"  🔧 Ремонт {group}{_short_time(e.time)} — {e.serial}")

    return "\n".join(lines)


def _recent(entries: List[ProcessedEvent], hours: int = RECENT_WINDOW_HOURS) -> List[ProcessedEvent]:
    cutoff = datetime.now() - timedelta(hours=hours)
    return [e for e in entries if datetime.fromisoformat(e.time) >= cutoff]


def _short_time(iso_time: str) -> str:
    return datetime.fromisoformat(iso_time).strftime("%H:%M")


def _truncate(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
