"""Фаза C — риск-менеджер: последний рубеж перед реальными деньгами.

Принципы (нарушение любого = потеря депозита быстрее, чем сработает стратегия):
  1. FAIL CLOSED — любая ошибка/неопределённость означает ОТКАЗ от сделки, не разрешение.
  2. ПЕРСИСТ состояния — рестарт контейнера НЕ сбрасывает дневной стоп. Иначе бот,
     упёршийся в лимит потерь, после перезапуска продолжит терять.
  3. Дневной стоп по UTC-суткам, kill-switch файлом (ручное вмешательство без деплоя),
     лимит одновременной экспозиции.

Kill-switch: `touch output/KILL` на VPS → бот немедленно перестаёт открывать позиции
(существующие доводятся по своим правилам). Снять: удалить файл.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from . import config, strategy


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class RiskState:
    day: str = field(default_factory=_today)
    realized_usd: float = 0.0        # реализованный PnL за UTC-сутки
    n_trades: int = 0
    n_wins: int = 0
    loss_streak: int = 0
    halted: bool = False
    halt_reason: str = ""
    halted_at: str = ""


class RiskManager:
    """Разрешает/запрещает открытие позиций. Состояние переживает рестарт."""

    def __init__(self, cfg: dict | None = None, path=None):
        r = {**strategy.RISK, **(cfg or {})}
        self.bankroll = float(r["BANKROLL_USD"])
        self.clip = float(r["CLIP_USD"])
        self.daily_stop = float(r["DAILY_STOP_FRAC"])
        self.max_positions = int(r["MAX_POSITIONS"])
        self.max_loss_streak = int(r["MAX_LOSS_STREAK"])
        self.mode = str(r["RISK_MODE"])        # 'shadow' (сбор данных) | 'enforce' (деньги)
        self.would_block = 0                   # сколько раз денежный лимит сработал бы в live
        self.path = path or (config.OUTPUT_DIR / "risk_state.json")
        self.kill_file = config.OUTPUT_DIR / str(r["KILL_FILE"])
        self.state = RiskState()
        self._load()

    # ---------- персист ----------
    def _load(self) -> None:
        try:
            if self.path.exists():
                self.state = RiskState(**json.loads(self.path.read_text(encoding="utf-8")))
        except Exception as e:  # noqa: BLE001
            # fail closed: не смогли прочитать состояние → считаем, что торговать нельзя
            self.state = RiskState(halted=True, halt_reason=f"битый risk_state: {type(e).__name__}")
        self._roll_day()

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(asdict(self.state), ensure_ascii=False), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            print(f"[risk] не сохранил состояние: {type(e).__name__}")

    def _roll_day(self) -> None:
        """Новые UTC-сутки → обнуляем дневной счёт и снимаем ДНЕВНОЙ стоп (только его)."""
        today = _today()
        if self.state.day != today:
            was_daily_halt = self.state.halted and self.state.halt_reason.startswith("дневной стоп")
            self.state = RiskState(day=today) if was_daily_halt else RiskState(
                day=today, halted=self.state.halted, halt_reason=self.state.halt_reason,
                halted_at=self.state.halted_at)
            self._save()

    # ---------- решения ----------
    def kill_switch_active(self) -> bool:
        try:
            return self.kill_file.exists()
        except Exception:  # noqa: BLE001
            return True          # fail closed: не смогли проверить → считаем, что стоп включён

    def can_open(self, open_positions: int) -> tuple[bool, str, bool]:
        """Разрешено ли открыть НОВУЮ позицию. → (можно, причина, жёсткое_ли_ограничение).

        hard=True — соблюдается ВСЕГДА (аварийный тормоз и лимит слотов из валидации).
        hard=False — денежные лимиты: в режиме 'shadow' (сбор данных на бумаге) они
        логируются, но не режут поток; в 'enforce' (реальные деньги) — блокируют.
        """
        self._roll_day()
        if self.kill_switch_active():
            return False, "KILL-SWITCH включён (файл)", True
        if open_positions >= self.max_positions:
            return False, f"лимит позиций {self.max_positions}", True
        if self.state.halted:
            return False, f"торговля остановлена: {self.state.halt_reason}", False
        exposure = (open_positions + 1) * self.clip
        if exposure > self.bankroll:
            return False, f"экспозиция ${exposure:.0f} > банка ${self.bankroll:.0f}", False
        return True, "", True

    def gate(self, open_positions: int) -> tuple[bool, str, bool]:
        """Итоговое решение с учётом режима. → (открывать, причина, было_бы_заблокировано)."""
        allowed, reason, hard = self.can_open(open_positions)
        if allowed:
            return True, "", False
        if hard or self.mode == "enforce":
            return False, reason, True
        self.would_block += 1
        return True, reason, True          # shadow: пропускаем, но фиксируем факт

    def on_close(self, pnl_frac: float) -> dict | None:
        """Учесть закрытую сделку. → dict с причиной, если СРАБОТАЛ стоп (иначе None)."""
        self._roll_day()
        pnl_usd = pnl_frac * self.clip
        s = self.state
        s.realized_usd += pnl_usd
        s.n_trades += 1
        if pnl_frac > 0:
            s.n_wins += 1
            s.loss_streak = 0
        else:
            s.loss_streak += 1

        tripped = None
        limit = -self.daily_stop * self.bankroll
        if s.realized_usd <= limit and not s.halted:
            s.halted = True
            s.halt_reason = (f"дневной стоп: ${s.realized_usd:.2f} <= ${limit:.2f} "
                             f"({self.daily_stop:.0%} банка)")
            s.halted_at = datetime.now(timezone.utc).isoformat()
            tripped = {"reason": s.halt_reason, "realized_usd": s.realized_usd}
        elif s.loss_streak >= self.max_loss_streak and not s.halted:
            # аварийный выключатель на случай СТРУКТУРНОЙ поломки (не обычной серии:
            # у стратегии ~57% убыточных сделок, длинные серии — норма, порог высокий)
            s.halted = True
            s.halt_reason = f"серия убытков {s.loss_streak} подряд — проверить систему"
            s.halted_at = datetime.now(timezone.utc).isoformat()
            tripped = {"reason": s.halt_reason, "loss_streak": s.loss_streak}
        self._save()
        return tripped

    def resume(self) -> None:
        """Ручное снятие остановки (после разбора причины)."""
        self.state.halted = False
        self.state.halt_reason = ""
        self.state.halted_at = ""
        self._save()

    def status(self) -> str:
        s = self.state
        wr = f"{s.n_wins}/{s.n_trades}" if s.n_trades else "0/0"
        flag = "🛑 СТОП" if (s.halted or self.kill_switch_active()) else "✅"
        wb = f" · в live заблокировано бы: {self.would_block}" if self.would_block else ""
        return (f"{flag} [{self.mode}] день {s.day}: PnL ${s.realized_usd:+.2f} · сделок {wr} · "
                f"серия минусов {s.loss_streak} · банк ${self.bankroll:.0f} клип ${self.clip:.0f}{wb}"
                + (f" · {s.halt_reason}" if s.halt_reason else ""))


if __name__ == "__main__":
    rm = RiskManager(path=config.OUTPUT_DIR / "_risk_selftest.json")
    print("старт:", rm.status())
    print("gate (0 позиций):", rm.gate(0))
    print("gate (на лимите):", rm.gate(rm.max_positions))
    for i in range(6):
        t = rm.on_close(-0.5)
        if t:
            print(f"  сделка {i+1}: СРАБОТАЛ СТОП → {t['reason']}")
            break
        print(f"  сделка {i+1}: PnL ${rm.state.realized_usd:+.2f}")
    print(f"после стопа gate [{rm.mode}]:", rm.gate(0))
    print("итог:", rm.status())
    rm.path.unlink(missing_ok=True)
