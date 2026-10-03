"""History of every player seen in the server log."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path


class PlayerHistory:
    def __init__(self, manager):
        self.path: Path = manager.paths.history_file
        self._lock = threading.Lock()
        try:
            self.data: dict = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def seen(self, steam_id: str, name: str) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            entry = self.data.setdefault(steam_id, {"first_seen": now})
            entry.update(name=name, last_seen=now)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)

    def list(self) -> list[dict]:
        with self._lock:
            rows = [{"id": sid, **info} for sid, info in self.data.items()]
        return sorted(rows, key=lambda r: str(r.get("last_seen", "")), reverse=True)
