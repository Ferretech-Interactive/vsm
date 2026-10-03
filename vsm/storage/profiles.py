"""Named snapshots of Game.ini, Engine.ini and the launch settings."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from .. import VsmError
from ..core.paths import child


class Profiles:
    def __init__(self, manager):
        self.m = manager
        self.dir: Path = manager.paths.profiles

    def list(self) -> list[str]:
        return sorted(p.name for p in self.dir.iterdir() if p.is_dir())

    def save(self, name: str) -> None:
        target = child(self.dir, name)
        target.mkdir(exist_ok=True)
        for src in (self.m.paths.game_ini, self.m.paths.engine_ini):
            if src.exists():
                shutil.copy2(src, target / src.name)
        launch = self.m.config.get().launch.model_dump()
        (target / "launch.json").write_text(json.dumps(launch, indent=2), encoding="utf-8")
        self.m.event("PROFILE", f"Saved profile '{name}'.")

    def load(self, name: str) -> None:
        source = child(self.dir, name)
        if not source.is_dir():
            raise VsmError("Profile not found.", 404)
        launch = source / "launch.json"
        if launch.exists():
            self.m.config.update({"launch": json.loads(launch.read_text(encoding="utf-8"))})
        self.m.paths.ini_dir.mkdir(parents=True, exist_ok=True)
        for dst in (self.m.paths.game_ini, self.m.paths.engine_ini):
            if (source / dst.name).exists():
                shutil.copy2(source / dst.name, dst)
        self.m.event("PROFILE", f"Loaded profile '{name}'.")

    def delete(self, name: str) -> None:
        target = child(self.dir, name)
        if not target.is_dir():
            raise VsmError("Profile not found.", 404)
        shutil.rmtree(target)
