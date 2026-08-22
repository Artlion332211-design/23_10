from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List

from .sheets_client import PositionStat
from .state import ProcessedEvent

RECENT_WINDOW_HOURS = 24

EVENT_LABELS = {
    "active": "🟢 В роботі",
    "repair": "🔧 На ремонт",
    "loss": "🔴 Втрата",
    "created": "🆕 Новий борт додано в реєстр",
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
    # "active"-подія трапляється лише при РЕАЛЬНІЙ зміні статусу (set_status
    # пропускає запис, якщо статус і так уже такий), тому це справді
    # "повернулось у стрій", а не кожна згадка в інвентарному переліку.
    returned = [e for e in recent if e.event_type == "active"]

    lines = [f"📊 Денний звіт станом на {now.strftime('%H:%M %d.%m.%Y')}", "", "Позиції:"]
    lines.append(format_position_stats(stats))
    lines.append("")
    lines.append(
        f"За останні {RECENT_WINDOW_HOURS} год: передано на ремонт — {len(repairs)}, "
        f"втрачено — {len(losses)}, повернулось у стрій — {len(returned)}"
    )
    for e in losses:
        group = f"[{e.group}] " if e.group else ""
        lines.append(f"  🔴 Втрата {group}{_short_time(e.time)} — {e.serial}")
    for e in repairs:
        group = f"[{e.group}] " if e.group else ""
        lines.append(f"  🔧 Ремонт {group}{_short_time(e.time)} — {e.serial}")
    for e in returned:
        group = f"[{e.group}] " if e.group else ""
        lines.append(f"  🟢 Повернувся {group}{_short_time(e.time)} — {e.serial}")

    return "\n".join(lines)


def format_not_in_service_list(rows: List[Dict[str, str]], limit: int = 40) -> str:
    if not rows:
        return "Усі борти в реєстрі зі статусом «в роботі»."

    by_status: Dict[str, List[Dict[str, str]]] = {}
    for row in rows:
        by_status.setdefault(row["status"], []).append(row)

    lines = [f"📋 Не в роботі: {len(rows)}", ""]
    shown = 0
    for status in sorted(by_status):
        group_rows = by_status[status]
        lines.append(f"{status} ({len(group_rows)}):")
        for row in group_rows:
            if shown >= limit:
                break
            group = f" [{row['group']}]" if row["group"] else ""
            lines.append(f"  {row['model']}{group} — ...{row['serial'][-5:]}")
            shown += 1
        lines.append("")
        if shown >= limit:
            break

    if len(rows) > shown:
        lines.append(f"... і ще {len(rows) - shown}, повний список — у самій таблиці.")

    return "\n".join(lines).strip()


def _recent(entries: List[ProcessedEvent], hours: int = RECENT_WINDOW_HOURS) -> List[ProcessedEvent]:
    cutoff = datetime.now() - timedelta(hours=hours)
    return [e for e in entries if datetime.fromisoformat(e.time) >= cutoff]


def _short_time(iso_time: str) -> str:
    return datetime.fromisoformat(iso_time).strftime("%H:%M")


def _truncate(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
