from __future__ import annotations

import json
import hashlib
import os
import logging
from dataclasses import dataclass
from pathlib import Path


DEFAULTS = {
    "workspace": str(Path(__file__).resolve().parents[5]),
    "strength": "保守",
    "quality_sensitivity": "标准",
    "delay_ms": 0,
    "analysis_workers": 0,
    "performance_version": 2,
    "page_size": 60,
    "set_size": 9,
    "second_cover": False,
    "all_review_large": True,
    "auto_advance": False,
}


@dataclass
class Runtime:
    home: Path

    @classmethod
    def default(cls):
        # Read the previous key for existing launchers; children receive BOYO_LOCAL.
        return cls(Path(os.environ.get("BOYO_LOCAL") or os.environ.get("LIUJI_LOCAL")
                        or Path(__file__).resolve().parents[3] / "local"))

    def prepare(self):
        for name in ("data", "cache/thumbs", "models", "logs", "tmp", "settings"):
            (self.home / name).mkdir(parents=True, exist_ok=True)

    @property
    def db_path(self):
        return self.home / "data" / "library.sqlite3"

    def database(self, workspace):
        from .portability import bound_database
        bound = bound_database(self, workspace)
        if bound is not None:
            return bound
        key = hashlib.sha256(str(Path(workspace).resolve()).casefold().encode()).hexdigest()[:16]
        return self.home / "data" / f"library-{key}.sqlite3"

    def settings(self):
        settings = dict(DEFAULTS)
        p = self.home / "settings" / "settings.json"
        if p.exists():
            try:
                saved = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(saved, dict): raise ValueError("设置应为对象")
                if "performance_version" not in saved and saved.get("delay_ms") == 60:
                    saved["delay_ms"] = 0
                settings.update(saved)
                settings["performance_version"] = 2
            except (OSError, ValueError):
                logging.exception("设置读取失败，使用默认设置；原设置文件保留")
        from .portability import relocated_settings
        return relocated_settings(self, settings)

    def save_settings(self, settings):
        from .settings_io import atomic_json
        atomic_json(self.home / "settings" / "settings.json", settings)
