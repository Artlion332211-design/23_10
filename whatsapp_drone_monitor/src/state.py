from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Union


@dataclass
class GroupState:
    on_position: int = 0
    launched_total: int = 0
    lost_total: int = 0
    returned_total: int = 0


@dataclass
class LogEntry:
    time: str
    group: str
    text: str
    keyword: Optional[str] = None


@dataclass
class FleetState:
    groups: Dict[str, GroupState] = field(default_factory=dict)
    losses: List[LogEntry] = field(default_factory=list)
    incidents: List[LogEntry] = field(default_factory=list)
    # chat_name -> текст останнього обробленого повідомлення в цьому чаті
    last_seen: Dict[str, str] = field(default_factory=dict)

    def group(self, name: str) -> GroupState:
        return self.groups.setdefault(name, GroupState())

    def record_launch(self, group: str) -> None:
        g = self.group(group)
        g.on_position += 1
        g.launched_total += 1

    def record_return(self, group: str) -> None:
        g = self.group(group)
        g.on_position = max(0, g.on_position - 1)
        g.returned_total += 1

    def record_loss(self, group: str, text: str, keyword: Optional[str]) -> None:
        g = self.group(group)
        g.on_position = max(0, g.on_position - 1)
        g.lost_total += 1
        self.losses.append(_log_entry(group, text, keyword))

    def record_incident(self, group: str, text: str, keyword: Optional[str]) -> None:
        self.incidents.append(_log_entry(group, text, keyword))

    def total_on_position(self) -> int:
        return sum(g.on_position for g in self.groups.values())

    def to_dict(self) -> dict:
        return {
            "groups": {name: asdict(g) for name, g in self.groups.items()},
            "losses": [asdict(e) for e in self.losses],
            "incidents": [asdict(e) for e in self.incidents],
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FleetState":
        state = cls()
        state.groups = {name: GroupState(**g) for name, g in data.get("groups", {}).items()}
        state.losses = [LogEntry(**e) for e in data.get("losses", [])]
        state.incidents = [LogEntry(**e) for e in data.get("incidents", [])]
        state.last_seen = data.get("last_seen", {})
        return state


def _log_entry(group: str, text: str, keyword: Optional[str]) -> LogEntry:
    return LogEntry(time=datetime.now().isoformat(timespec="seconds"), group=group, text=text, keyword=keyword)


def load_state(path: Union[str, Path]) -> FleetState:
    p = Path(path)
    if not p.exists():
        return FleetState()
    return FleetState.from_dict(json.loads(p.read_text(encoding="utf-8")))


def save_state(state: FleetState, path: Union[str, Path]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Атомарний запис (tmp-файл + rename), щоб збій чи Ctrl+C посеред запису
    # не лишив state.json пошкодженим і не обнулив лічильники на позиції.
    fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=".state_", suffix=".tmp")
    with open(fd, "w", encoding="utf-8") as f:
        json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)
    Path(tmp_name).replace(p)
