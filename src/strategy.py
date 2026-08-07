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
    "entry": ["RULE", "PRIORITY_ACTORS"],
    "exit": ["PARTIAL_TAKES", "TP_MULT", "SL_MULT", "TRAIL", "TRAIL_ARM",
             "DEAD_AGE_H", "MAX_HOLD_S", "EXIT_ACTOR_FRAC"],
    "risk": ["MAX_POSITIONS", "EXIT_FEE", "BANKROLL_USD", "CLIP_USD",
             "DAILY_STOP_FRAC", "MAX_LOSS_STREAK", "KILL_FILE", "RISK_MODE"],
    "alerts": ["QUALITY_MIN_MC", "QUALITY_MIN_VELOCITY", "SANITY_JUMP",
               "STALE_SIGNAL_H", "MAX_ANOMALY_RATE", "PRIORITY_SILENCE_H",
               "WATCHLIST_MAX_AGE_D"],
    "tracking": ["TICK_S", "TRACK_S", "WS_CONNECTIONS", "RPC_PROVIDER", "MAX_SUBS_PER_CONN", "HIGH_RATE_WALLETS"],
    "execution": ["SHADOW_CLIP_USD", "SLIPPAGE_BPS", "SHADOW_ENABLED", "LIVE_ENABLED", "SEND_PROVIDER",
                  "MAX_SWAP_USD", "MAX_PRIORITY_LAMPORTS", "PRIORITY_LEVEL",
                  "CONFIRM_TIMEOUT_S"],
    "sweep": ["ENABLED", "DRY_RUN", "SWEEP_ADDRESS", "SWEEP_TRIGGER_USD", "SWEEP_MIN_USD",
              "SWEEP_MAX_USD", "MIN_INTERVAL_S", "FEE_BUFFER_SOL"],
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
    en = cfg["entry"]
    if en["RULE"] not in ("all", "strong_or_priority"):
        raise ValueError(f"strategy.yaml: entry.RULE = {en['RULE']!r}, ожидается all|strong_or_priority")
    if en["RULE"] == "strong_or_priority" and not en["PRIORITY_ACTORS"]:
        raise ValueError("strategy.yaml: RULE=strong_or_priority, но PRIORITY_ACTORS пуст")
    s = cfg["signal"]
    if s["CONFLUENCE_N"] > s["STRONG_CONFLUENCE_N"]:
        raise ValueError("strategy.yaml: CONFLUENCE_N > STRONG_CONFLUENCE_N")
    r = cfg["risk"]
    if r["MAX_POSITIONS"] < 1:
        raise ValueError("strategy.yaml: MAX_POSITIONS >= 1")
    if r["CLIP_USD"] <= 0 or r["BANKROLL_USD"] <= 0:
        raise ValueError("strategy.yaml: CLIP_USD и BANKROLL_USD должны быть > 0")
    if not (0 < r["DAILY_STOP_FRAC"] <= 1):
        raise ValueError("strategy.yaml: DAILY_STOP_FRAC∈(0,1]")
    clips = r["BANKROLL_USD"] / r["CLIP_USD"]
    if clips < 15:
        # risk-of-ruin (MC на реальном распределении): <15 клипов → разорение до прихода хвоста
        raise ValueError(f"strategy.yaml: банк = {clips:.0f} клипов, нужно >=15 "
                         f"(risk-of-ruin: при 5 клипах разорение 20%)")
    if r["MAX_POSITIONS"] * r["CLIP_USD"] > r["BANKROLL_USD"]:
        raise ValueError("strategy.yaml: MAX_POSITIONS × CLIP_USD > BANKROLL_USD")
    ex = cfg["execution"]
    if ex["MAX_SWAP_USD"] < r["CLIP_USD"]:
        raise ValueError("strategy.yaml: MAX_SWAP_USD меньше CLIP_USD — торговать нечем")
    if ex["MAX_SWAP_USD"] > r["BANKROLL_USD"] / 2:
        # один своп не должен уносить половину банка даже при ошибке вызова
        raise ValueError(f"strategy.yaml: MAX_SWAP_USD ${ex['MAX_SWAP_USD']} > половины банка")
    if ex["PRIORITY_LEVEL"] not in ("medium", "high", "veryHigh"):
        raise ValueError("strategy.yaml: PRIORITY_LEVEL = medium|high|veryHigh")
    if ex["SEND_PROVIDER"] not in ("helius", "public", "drpc", "jito"):
        raise ValueError("strategy.yaml: SEND_PROVIDER должен быть helius, drpc, jito или public")
    if ex["LIVE_ENABLED"] and ex["SEND_PROVIDER"] == "public":
        raise ValueError("strategy.yaml: живая торговля через публичный узел запрещена — "
                         "он лимитирует sendTransaction и может не разослать сделку")
    if ex["LIVE_ENABLED"] and cfg["risk"]["RISK_MODE"] != "enforce":
        # реальные деньги без жёстких лимитов = дневной стоп не остановит слив
        raise ValueError("strategy.yaml: LIVE_ENABLED=true требует RISK_MODE=enforce")
    sw = cfg["sweep"]
    if sw["ENABLED"] and not str(sw["SWEEP_ADDRESS"]).strip():
        raise ValueError("strategy.yaml: sweep.ENABLED=true, но SWEEP_ADDRESS пуст")
    if sw["SWEEP_TRIGGER_USD"] <= r["BANKROLL_USD"]:
        raise ValueError("strategy.yaml: SWEEP_TRIGGER_USD должен быть выше BANKROLL_USD")
    if sw["SWEEP_TRIGGER_USD"] > r["BANKROLL_USD"] + sw["SWEEP_MIN_USD"] * 2:
        # иначе шаг вывода не применяется: каждый вывод будет размером с разрыв порога
        raise ValueError(f"strategy.yaml: порог ${sw['SWEEP_TRIGGER_USD']} слишком высоко над "
                         f"банком ${r['BANKROLL_USD']} — шаг ${sw['SWEEP_MIN_USD']} не сработает")
    if sw["SWEEP_MIN_USD"] <= 0 or sw["SWEEP_MAX_USD"] < sw["SWEEP_MIN_USD"]:
        raise ValueError("strategy.yaml: 0 < SWEEP_MIN_USD <= SWEEP_MAX_USD")
    if not (1 <= cfg["tracking"]["WS_CONNECTIONS"] <= 5):
        raise ValueError("strategy.yaml: WS_CONNECTIONS вне 1..5 (лимит Helius free)")
    if cfg["tracking"]["RPC_PROVIDER"] not in ("helius", "public"):
        raise ValueError("strategy.yaml: RPC_PROVIDER должен быть helius или public")
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
ENTRY = CFG["entry"]
EXIT = CFG["exit"]
RISK = CFG["risk"]
ALERTS = CFG["alerts"]
TRACKING = CFG["tracking"]
EXECUTION = CFG["execution"]
SWEEP = CFG["sweep"]


if __name__ == "__main__":
    print(f"strategy.yaml OK — version {VERSION}")
    for name, sec in (("signal", SIGNAL), ("entry", ENTRY), ("exit", EXIT), ("risk", RISK),
                      ("alerts", ALERTS), ("tracking", TRACKING), ("execution", EXECUTION),
                      ("sweep", SWEEP)):
        print(f"  {name}: {sec}")
