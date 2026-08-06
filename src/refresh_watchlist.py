"""Еженедельное обновление watchlist: перезапуск discovery Контура 1 + отчёт об изменениях.

Зачем еженедельно: замерено — 26 из 68 акторов (38%) молчат уже через 27 дней после сборки
списка. Рынок мем-коинов меняется быстро, операторы уходят и появляются. Цена перезапуска
(~110 кредитов Dune из 2500/мес) несопоставима с ценой торговли по устаревшему списку.

Запускается ЛОКАЛЬНО (нужны dune-client/duckdb/pandas — их нет в контейнере монитора),
затем новый flow_watchlist.json заливается на VPS.

Run:  .venv\\Scripts\\python.exe -m src.refresh_watchlist --days 30 [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

PY = sys.executable
WL = config.OUTPUT_DIR / "flow_watchlist.json"


def _load(path: Path) -> dict[str, dict]:
    """actor_id -> {wallets, weight}."""
    if not path.exists():
        return {}
    return {a["actor_id"]: a for a in json.loads(path.read_text(encoding="utf-8"))}


def _run(mod: str, args: list[str]) -> bool:
    print(f"\n=== {mod} {' '.join(args)} ===")
    r = subprocess.run([PY, "-m", f"src.{mod}", *args], cwd=str(config.ROOT))
    if r.returncode != 0:
        print(f"[!] {mod} завершился с кодом {r.returncode}")
        return False
    return True


def diff_report(old: dict, new: dict) -> str:
    added = set(new) - set(old)
    gone = set(old) - set(new)
    kept = set(old) & set(new)
    lines = [f"акторов было {len(old)} → стало {len(new)}",
             f"  сохранилось {len(kept)}, новых {len(added)}, выбыло {len(gone)}"]
    if added:
        lines.append("  НОВЫЕ: " + ", ".join(a[:10] + "…" for a in sorted(added)[:8]))
    if gone:
        lines.append("  ВЫБЫЛИ: " + ", ".join(a[:10] + "…" for a in sorted(gone)[:8]))
    w_old = sum(len(v["wallets"]) for v in old.values())
    w_new = sum(len(v["wallets"]) for v in new.values())
    lines.append(f"  кошельков {w_old} → {w_new}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Еженедельное обновление watchlist")
    ap.add_argument("--days", type=int, default=30, help="глубина окна для universe")
    ap.add_argument("--dry-run", action="store_true", help="только показать план и текущий список")
    ap.add_argument("--skip-dune", action="store_true",
                    help="не дёргать Dune, пересобрать watchlist из кэша DuckDB")
    args = ap.parse_args()

    old = _load(WL)
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")
    print(f"текущий watchlist: {len(old)} акторов / "
          f"{sum(len(v['wallets']) for v in old.values())} кошельков")
    if WL.exists():
        age = (datetime.now(timezone.utc).timestamp() - WL.stat().st_mtime) / 86400
        print(f"возраст файла: {age:.1f} дней")
    print(f"окно discovery: с {since}")

    if args.dry_run:
        print("\n[dry-run] шаги, которые были бы выполнены:")
        print(f"  1. src.universe --since {since} --append      (~85 кредитов Dune)")
        print("  2. src.aggregate                              (~24 кредита)")
        print("  3. src.flow_watchlist                         (локально, из кэша)")
        print("  4. проверка изменений + деплой на VPS")
        return

    backup = WL.with_suffix(".json.bak")
    if WL.exists():
        shutil.copy(WL, backup)
        print(f"бэкап: {backup.name}")

    if not args.skip_dune:
        if not _run("universe", ["--since", since, "--append"]):
            return
        if not _run("aggregate", []):
            return
    if not _run("flow_watchlist", []):
        print("[!] сборка не удалась — старый watchlist сохранён")
        if backup.exists():
            shutil.copy(backup, WL)
        return

    new = _load(WL)
    if not new:
        print("[!] новый watchlist ПУСТ — откатываюсь на старый")
        if backup.exists():
            shutil.copy(backup, WL)
        return
    print("\n=== ЧТО ИЗМЕНИЛОСЬ ===")
    print(diff_report(old, new))
    print("\nдалее: залить на VPS и перезапустить монитор:")
    print("  scp output/flow_watchlist.json vps-trader:/opt/smart-money/output/")
    print("  ssh vps-trader 'cd /opt/smart-money && docker compose restart'")
    print("ПОСЛЕ обновления: перепроверить PRIORITY_ACTORS в config/strategy.yaml —")
    print("приоритетный актор мог выбыть, тогда правило входа надо пересчитать на свежих данных.")


if __name__ == "__main__":
    main()
