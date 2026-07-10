"""Единая загрузка конфигурации: секреты из .env + пороги из params.yaml.

Секреты доступны только как атрибуты рантайма; модуль их НИКОГДА не логирует и не печатает.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Windows-консоль (cp1251) не кодирует часть Unicode → защищаемся от падений stdout.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def secret(name: str, required: bool = True) -> str | None:
    """Достать секрет из окружения. Значение не логируется."""
    val = os.getenv(name)
    if required and not val:
        raise RuntimeError(f"{name} не задан в .env")
    return val


def load_params(path: Path | None = None) -> dict[str, Any]:
    """Прочитать config/params.yaml (пороги Контура 1)."""
    p = path or (ROOT / "config" / "params.yaml")
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# Пути проекта
OUTPUT_DIR = ROOT / "output"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "smart_money.duckdb"

for _d in (OUTPUT_DIR, DATA_DIR):
    _d.mkdir(exist_ok=True)
