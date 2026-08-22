from __future__ import annotations

from datetime import datetime, timedelta
from typing import List

from .state import FleetState, LogEntry

RECENT_WINDOW_HOURS = 24


def format_status_report(state: FleetState) -> str:
    now = datetime.now()
    lines = [f"\U0001F4CA Звіт по БпЛА станом на {now.strftime('%H:%M %d.%m.%Y')}", ""]
    lines.append(f"На позиції зараз: {state.total_on_position()} борт(и)")
    for name in sorted(state.groups):
        g = state.groups[name]
        if g.on_position or g.launched_total or g.lost_total:
            lines.append(f"  {name}: {g.on_position}")

    recent_losses = _recent(state.losses)
    recent_incidents = _recent(state.incidents)

    lines.append("")
    lines.append(
        f"За останні {RECENT_WINDOW_HOURS} год: втрат — {len(recent_losses)}, "
        f"нештатних ситуацій — {len(recent_incidents)}"
    )
    for e in recent_losses:
        lines.append(f"  ⚠️ Втрата [{e.group}] {_short_time(e.time)} — {_truncate(e.text)}")
    for e in recent_incidents:
        lines.append(f"  ⚠️ НС [{e.group}] {_short_time(e.time)} — {_truncate(e.text)}")

    return "\n".join(lines)


def format_alert(event_label: str, group: str, text: str) -> str:
    return f"\U0001F6A8 {event_label} — {group}\n{_truncate(text, 300)}"


def _recent(entries: List[LogEntry], hours: int = RECENT_WINDOW_HOURS) -> List[LogEntry]:
    cutoff = datetime.now() - timedelta(hours=hours)
    return [e for e in entries if datetime.fromisoformat(e.time) >= cutoff]


def _short_time(iso_time: str) -> str:
    return datetime.fromisoformat(iso_time).strftime("%H:%M")


def _truncate(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
