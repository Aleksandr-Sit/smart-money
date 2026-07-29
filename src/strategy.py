"""Единый загрузчик параметров стратегии (config/strategy.yaml) + валидация.

Зачем: до аудита-4 параметры жили в 4 разных модулях, версия конфига нигде не
фиксировалась → нельзя было сказать, каким конфигом порождены данные. Теперь один
источник правды, VERSION пишется в каждый сигнал/выход.

Валидация на старте — жёсткая: битый конфиг = падение процесса, а не тихая торговля
неправильными порогами (для реальных денег это принципиально).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
PATH = ROOT / "config" / "strategy.yaml"

_REQUIRED = {
    "signal": ["CONFLUENCE_N", "STRONG_CONFLUENCE_N", "CONFLUENCE_WINDOW_S",
               "SIGNAL_MAX_MC_USD", "SIGNAL_MAX_AGE_S", "SIGNAL_MIN_USD", "QUIET_MAX_USD"],
    "exit": ["PARTIAL_TAKES", "TP_MULT", "SL_MULT", "TRAIL", "TRAIL_ARM",
             "DEAD_AGE_H", "MAX_HOLD_S", "EXIT_ACTOR_FRAC"],
    "risk": ["MAX_POSITIONS", "EXIT_FEE"],
    "alerts": ["QUALITY_MIN_MC", "QUALITY_MIN_VELOCITY", "SANITY_JUMP",
               "STALE_SIGNAL_H", "MAX_ANOMALY_RATE"],
    "tracking": ["TICK_S", "TRACK_S"],
}


def _validate(cfg: dict[str, Any]) -> None:
    if not cfg.get("version"):
        raise ValueError("strategy.yaml: нет поля version")
    for section, keys in _REQUIRED.items():
        if section not in cfg:
            raise ValueError(f"strategy.yaml: нет секции '{section}'")
        for k in keys:
            if k not in cfg[section]:
                raise ValueError(f"strategy.yaml: нет параметра {section}.{k}")
    e = cfg["exit"]
    if not (0 < e["SL_MULT"] < 1 < e["TP_MULT"]):
        raise ValueError("strategy.yaml: должно быть 0 < SL_MULT < 1 < TP_MULT")
    if not (0 < e["TRAIL"] < 1) or e["TRAIL_ARM"] < 1:
        raise ValueError("strategy.yaml: TRAIL∈(0,1), TRAIL_ARM>=1")
    if not (0 < e["EXIT_ACTOR_FRAC"] <= 1):
        raise ValueError("strategy.yaml: EXIT_ACTOR_FRAC∈(0,1]")
    total = 0.0
    for item in e["PARTIAL_TAKES"]:
        if len(item) != 2:
            raise ValueError("strategy.yaml: PARTIAL_TAKES = [[множитель, доля], ...]")
        mult, frac = item
        if mult <= 1 or not (0 < frac <= 1):
            raise ValueError(f"strategy.yaml: некорректный частичный тейк {item}")
        total += frac
    if total > 1.0 + 1e-9:
        raise ValueError(f"strategy.yaml: сумма долей PARTIAL_TAKES = {total} > 1")
    s = cfg["signal"]
    if s["CONFLUENCE_N"] > s["STRONG_CONFLUENCE_N"]:
        raise ValueError("strategy.yaml: CONFLUENCE_N > STRONG_CONFLUENCE_N")
    if cfg["risk"]["MAX_POSITIONS"] < 1:
        raise ValueError("strategy.yaml: MAX_POSITIONS >= 1")
    if cfg["tracking"]["TICK_S"] > 20:
        # на 90с edge исчезал (аудит-3): гранулярность выхода — часть стратегии, не деталь
        raise ValueError("strategy.yaml: TICK_S > 20с — на такой гранулярности edge не выживает")


def load(path: Path | None = None) -> dict[str, Any]:
    with open(path or PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    _validate(cfg)
    # списки → кортежи (частичные тейки удобнее как неизменяемые пары)
    cfg["exit"]["PARTIAL_TAKES"] = [tuple(x) for x in cfg["exit"]["PARTIAL_TAKES"]]
    return cfg


CFG = load()
VERSION: str = CFG["version"]
SIGNAL = CFG["signal"]
EXIT = CFG["exit"]
RISK = CFG["risk"]
ALERTS = CFG["alerts"]
TRACKING = CFG["tracking"]


if __name__ == "__main__":
    print(f"strategy.yaml OK — version {VERSION}")
    for name, sec in (("signal", SIGNAL), ("exit", EXIT), ("risk", RISK),
                      ("alerts", ALERTS), ("tracking", TRACKING)):
        print(f"  {name}: {sec}")
