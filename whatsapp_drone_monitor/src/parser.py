from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import List, Optional

# DJI-серійники в наявних повідомленнях завжди 20-символьні (цифри+великі
# літери), напр. 1581F7K3C264200DAFYJ. Діапазон 16-24 лишає трохи запасу на
# інші моделі, не захоплюючи випадково координати чи звичайний текст.
SERIAL_RE = re.compile(r"^[0-9A-Za-z]{16,24}$")
SERIAL_INLINE_RE = re.compile(r"^([0-9A-Za-z]{16,24})\s*[-–—:]\s*\(?([^)]*)\)?\s*$")

GROUP_HEADER_RE = re.compile(r'^Груп[аи]\s*:?\s*["«]?([^"»]+?)["»]?\s*$', re.IGNORECASE)
GROUP_STOCK_RE = re.compile(r'["«]([^"»]+)["»]\s+в наявності', re.IGNORECASE)
LOSS_HEADER_RE = re.compile(r"втрата\s+борт", re.IGNORECASE)
REPAIR_PHRASE_RE = re.compile(r"ремонт", re.IGNORECASE)
LOSS_PHRASE_RE = re.compile(r"втрат|збил[аи]|збито|підбил[аи]|підбито", re.IGNORECASE)
MODEL_RE = re.compile(r"\b(Matrice\s*4T|Matrice\s*4E|М4Т|М4Е|M4T|M4E)\b", re.IGNORECASE)
FIELD_RE = re.compile(
    r"^(дата|час|орієнтовні координати|координати|причина|пілот)\s*:?\s*(.*)$", re.IGNORECASE
)


class EventType(enum.Enum):
    REPAIR = "repair"
    LOSS = "loss"


@dataclass
class DroneEvent:
    event_type: EventType
    serial: str
    group: Optional[str] = None
    model: Optional[str] = None
    pilot: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = None
    coordinates: Optional[str] = None
    reason: Optional[str] = None
    note: Optional[str] = None
    raw: str = ""


def parse_message(text: str) -> List[DroneEvent]:
    """Витягує статусні події (ремонт/втрата) з повідомлення групи.

    Просте перерахування серійників у складі інвентарного звіту (без
    жодної позначки) НЕ породжує подій — лише явний тригер біля серійника
    (ремонт/втрата) міняє статус, інакше кожен щоденний перелік наявності
    перетирав би статуси в таблиці.
    """
    lines = text.splitlines()
    n = len(lines)
    events: List[DroneEvent] = []
    current_group: Optional[str] = None

    i = 0
    while i < n:
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        header_match = GROUP_HEADER_RE.match(line) or GROUP_STOCK_RE.search(line)
        if header_match:
            current_group = header_match.group(1).strip()
            i += 1
            continue

        if LOSS_HEADER_RE.search(line):
            block_lines = []
            j = i + 1
            while j < n and lines[j].strip():
                block_lines.append(lines[j].strip())
                j += 1

            pilot_before = None
            if i > 0 and lines[i - 1].strip():
                m = FIELD_RE.match(lines[i - 1].strip())
                if m and m.group(1).lower() == "пілот":
                    pilot_before = m.group(2).strip()

            event = _parse_loss_block(block_lines, group=current_group, pilot=pilot_before)
            if event:
                events.append(event)
            i = j
            continue

        inline = SERIAL_INLINE_RE.match(line)
        if inline:
            serial, note = inline.group(1).upper(), inline.group(2).strip()
            event_type = _classify_note(note)
            if event_type:
                events.append(
                    DroneEvent(event_type, serial=serial, group=current_group, note=note, raw=line)
                )
            i += 1
            continue

        if SERIAL_RE.match(line):
            next_line = lines[i + 1].strip() if i + 1 < n else ""
            # Якщо наступний рядок сам є серійником (голим чи з анотацією),
            # анотація належить ЙОМУ, а не поточному — інакше остання
            # позначка в інвентарному переліку "прилипає" до попереднього
            # борта замість свого власного.
            next_is_serial_line = bool(SERIAL_RE.match(next_line) or SERIAL_INLINE_RE.match(next_line))
            event_type = _classify_note(next_line) if next_line and not next_is_serial_line else None
            if event_type:
                events.append(
                    DroneEvent(
                        event_type,
                        serial=line.upper(),
                        group=current_group,
                        note=next_line,
                        raw=f"{line}\n{next_line}",
                    )
                )
                i += 2
                continue
            i += 1
            continue

        i += 1

    return events


def _classify_note(note: str) -> Optional[EventType]:
    if REPAIR_PHRASE_RE.search(note):
        return EventType.REPAIR
    if LOSS_PHRASE_RE.search(note):
        return EventType.LOSS
    return None


def _parse_loss_block(lines: List[str], group: Optional[str], pilot: Optional[str]) -> Optional[DroneEvent]:
    serial = None
    model = None
    fields = {}
    pending_label = None

    for line in lines:
        if serial is None and SERIAL_RE.match(line):
            serial = line.upper()
            continue

        model_match = MODEL_RE.search(line)
        if model_match and model is None:
            model = model_match.group(1)

        field_match = FIELD_RE.match(line)
        if field_match:
            label, value = field_match.group(1).lower(), field_match.group(2).strip()
            if value:
                fields[label] = value
                pending_label = None
            else:
                pending_label = label
            continue

        if pending_label:
            fields[pending_label] = line
            pending_label = None

    if serial is None:
        return None

    return DroneEvent(
        event_type=EventType.LOSS,
        serial=serial,
        group=group,
        model=model,
        pilot=fields.get("пілот", pilot),
        date=fields.get("дата"),
        time=fields.get("час"),
        coordinates=fields.get("орієнтовні координати") or fields.get("координати"),
        reason=fields.get("причина"),
        raw="\n".join(lines),
    )
