from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Union


@dataclass
class ProcessedEvent:
    time: str
    event_type: str  # "repair" / "loss" / "not_found"
    serial: str
    group: Optional[str] = None
    sheet: Optional[str] = None
    old_status: Optional[str] = None
    new_status: Optional[str] = None
    note: Optional[str] = None


@dataclass
class BotState:
    # chat_name -> текст останнього обробленого повідомлення в цьому чаті
    last_seen: Dict[str, str] = field(default_factory=dict)
    events: List[ProcessedEvent] = field(default_factory=list)

    def record_event(self, **kwargs) -> ProcessedEvent:
        entry = ProcessedEvent(time=datetime.now().isoformat(timespec="seconds"), **kwargs)
        self.events.append(entry)
        return entry

    def to_dict(self) -> dict:
        return {
            "last_seen": self.last_seen,
            "events": [asdict(e) for e in self.events],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BotState":
        state = cls()
        state.last_seen = data.get("last_seen", {})
        state.events = [ProcessedEvent(**e) for e in data.get("events", [])]
        return state


def load_state(path: Union[str, Path]) -> BotState:
    p = Path(path)
    if not p.exists():
        return BotState()
    return BotState.from_dict(json.loads(p.read_text(encoding="utf-8")))


def save_state(state: BotState, path: Union[str, Path]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Атомарний запис (tmp-файл + rename), щоб збій чи Ctrl+C посеред запису
    # не лишив state.json пошкодженим.
    fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=".state_", suffix=".tmp")
    with open(fd, "w", encoding="utf-8") as f:
        json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)
    Path(tmp_name).replace(p)
