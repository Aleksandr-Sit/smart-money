"""Автономное еженедельное обновление watchlist на VPS — без участия человека.

Запускается кроном, сам тратит кредиты Dune, сам присылает итог в Telegram.
Человек НЕ подтверждает запуск — поэтому защиты обязательны:

  1. ПРОВЕРКА КРЕДИТОВ ДО СТАРТА — если меньше запаса, не начинаем вовсе.
     Иначе автозапуск мог бы выесть месячный лимит и оставить проект без discovery.
  2. Только DUNE_API_KEY. Второй ключ (DUNE_API_KEY2) НЕ используется — это против ToS
     Dune (мультиаккаунт ради кредитов), риск бана обоих аккаунтов.
  3. Бэкап watchlist и АВТООТКАТ при пустом/сломанном результате: торговать по битому
     списку опаснее, чем по устаревшему.
  4. Итог в Telegram В ЛЮБОМ случае — включая отказ и ошибку. Молчание недопустимо:
     владелец должен знать, что список НЕ обновился.

Run (в контейнере discovery):  python -m src.auto_refresh --days 30
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import requests

from . import config, delivery, strategy
from .refresh_watchlist import WL, _load, diff_report

CREDITS_NEEDED = 150      # оценка расхода (~110) + запас на разброс
CREDITS_FLOOR = 300       # ниже этого остатка не запускаемся вовсе


def credits_left() -> float | None:
    """Остаток кредитов Dune. None = не смогли узнать (тогда НЕ запускаемся)."""
    try:
        r = requests.post("https://api.dune.com/api/v1/usage",
                          headers={"X-Dune-API-Key": config.secret("DUNE_API_KEY")},
                          timeout=30).json()
        for b in r.get("billing_periods", []):
            used, inc = b.get("credits_used"), b.get("credits_included")
            if used is not None and inc:
                return inc - used
    except Exception:  # noqa: BLE001
        return None
    return None


def _run(mod: str, args: list[str], timeout: int = 3600) -> tuple[bool, str]:
    try:
        r = subprocess.run([sys.executable, "-m", f"src.{mod}", *args],
                           cwd=str(config.ROOT), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"{mod}: таймаут {timeout}с"
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "")[-300:]
        return False, f"{mod}: код {r.returncode} · {tail}"
    return True, ""


def main() -> None:
    ap = argparse.ArgumentParser(description="Автономное обновление watchlist")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--force", action="store_true", help="игнорировать порог кредитов")
    args = ap.parse_args()

    before = credits_left()
    old = _load(WL)
    age_d = ((datetime.now(timezone.utc).timestamp() - WL.stat().st_mtime) / 86400) if WL.exists() else 999

    if before is None:
        delivery.send_alert("автообновление watchlist ОТМЕНЕНО: не удалось узнать остаток "
                            "кредитов Dune. Список остался прежним "
                            f"({len(old)} акторов, возраст {age_d:.0f} дн)")
        return
    if before < CREDITS_FLOOR and not args.force:
        delivery.send_alert(f"автообновление watchlist ОТМЕНЕНО: кредитов Dune {before:.0f} "
                            f"(нужен запас {CREDITS_FLOOR}). Список прежний: {len(old)} акторов, "
                            f"возраст {age_d:.0f} дн. Обновится после сброса лимита")
        return

    backup = WL.with_suffix(".json.bak")
    if WL.exists():
        shutil.copy(WL, backup)

    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")
    for mod, margs in (("universe", ["--since", since, "--append"]),
                       ("aggregate", []), ("flow_watchlist", [])):
        ok, err = _run(mod, margs)
        if not ok:
            if backup.exists():
                shutil.copy(backup, WL)          # откат: старый список лучше сломанного
            delivery.send_alert(f"автообновление watchlist ПРОВАЛИЛОСЬ на шаге {mod}.\n{err}\n"
                                f"Откат на прежний список ({len(old)} акторов, {age_d:.0f} дн)")
            return

    new = _load(WL)
    if not new or len(new) < 5:
        if backup.exists():
            shutil.copy(backup, WL)
        delivery.send_alert(f"автообновление watchlist: результат пустой/подозрительный "
                            f"({len(new)} акторов) → ОТКАТ на прежний ({len(old)})")
        return

    after = credits_left()
    spent = (before - after) if after is not None else None
    gone_priority = [a for a in strategy.ENTRY["PRIORITY_ACTORS"] if a not in new]

    msg = ["🔄 WATCHLIST ОБНОВЛЁН автоматически", "", diff_report(old, new), ""]
    if spent is not None:
        msg.append(f"кредитов потрачено: {spent:.0f}, осталось {after:.0f}")
    if gone_priority:
        msg.append("")
        msg.append("🚨 ПРИОРИТЕТНЫЙ АКТОР ВЫБЫЛ ИЗ СПИСКА: "
                   + ", ".join(a[:10] + "…" for a in gone_priority))
        msg.append("На нём держится лучшая когорта (медиана +68%). "
                   "Правило входа надо пересчитать на свежих данных!")
    else:
        msg.append("приоритетные акторы на месте ✅")
    msg.append("")
    msg.append("монитор будет перезапущен для применения списка")
    delivery.send_telegram("\n".join(msg))
    print("REFRESH_OK")          # маркер успеха для крон-скрипта (он перезапустит монитор)


if __name__ == "__main__":
    main()
