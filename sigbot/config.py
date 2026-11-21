from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


class Config(dict):
    """Словарь с доступом через точку: cfg.risk.risk_min_pct."""

    def __getattr__(self, key: str) -> Any:
        try:
            v = self[key]
        except KeyError as e:
            raise AttributeError(key) from e
        return Config(v) if isinstance(v, dict) else v


def load(path: str | Path | None = None) -> Config:
    p = Path(path or os.environ.get("SIGBOT_CONFIG", DEFAULT_PATH))
    with open(p, encoding="utf-8") as f:
        return Config(yaml.safe_load(f))


def secret(name: str) -> str | None:
    """Секреты только из окружения, в репозитории их нет."""
    return os.environ.get(name)
