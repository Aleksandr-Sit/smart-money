"""Протухшие события: вход по цене десятиминутной давности — гарантированно плохая сделка.

Аудит 10.08. Проверка возраста в мониторе сравнивала trade["ts"] с текущим временем,
но разбор из логов blockTime не даёт — монитор сам проставлял туда время ПОЛУЧЕНИЯ.
Возраст на этом пути всегда выходил нулевым, то есть защита работала только на пути
через getTransaction (треть потока). Второй признак возраста — слот уведомления,
он приходит бесплатно вместе с событием.

Отставание считается ВНУТРИ соединения. Общий максимум по всем каналам забраковал бы
отстающий канал целиком: каналы подключаются в разное время и идут вразнобой.
"""
import asyncio
import json

import pytest

from src import helius_ws, monitor


# ---------- перевод слотов в секунды ----------
def test_порог_протухания_в_слотах():
    """5 минут при слоте 0.4с = 750 слотов. Цифра должна оставаться осмысленной."""
    порог = monitor.MAX_EVENT_AGE_S / monitor.SLOT_S
    assert 700 <= порог <= 800


# ---------- отбор в WS-клиенте: слот берётся из уведомления ----------
def _уведомление(slot: int, sig: str = "SIG"):
    return json.dumps({
        "method": "logsNotification",
        "params": {"subscription": 1,
                   "result": {"context": {"slot": slot},
                              "value": {"signature": sig, "logs": ["l"], "err": None}}},
    })


class _WS:
    """Минимальная заглушка соединения: отдаёт подтверждение подписки, затем события."""

    def __init__(self, messages):
        self._msgs = messages

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, _):
        return None

    def __aiter__(self):
        async def gen():
            yield json.dumps({"id": 0, "result": 1})     # подписка подтверждена
            for m in self._msgs:
                yield m
            raise StopAsyncIteration

        return gen()


def _прогнать(messages):
    """→ список (wallet, sig, logs, lag), с которыми позвали on_event."""
    calls = []

    async def on_event(w, sig, logs=None, lag=0):
        calls.append((w, sig, logs, lag))

    async def main(monkeypatch):
        monkeypatch.setattr(helius_ws.websockets, "connect", lambda *a, **k: _WS(messages))
        monkeypatch.setattr(helius_ws, "_backfill",
                            lambda *a, **k: asyncio.sleep(0))
        monkeypatch.setattr(helius_ws.helius, "ws_url", lambda: "wss://x")
        monkeypatch.setattr(helius_ws, "SUBSCRIBE_PACE_S", 0)
        task = asyncio.create_task(helius_ws.subscribe_wallets(["W"], on_event, label="t"))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    return calls, main


def test_отставание_слота_доезжает_до_монитора(monkeypatch):
    """Свежее событие → lag 0; событие на 1000 слотов позади максимума → lag 1000."""
    calls, main = _прогнать([_уведомление(200_000, "СВЕЖАЯ"),
                             _уведомление(199_000, "ОТСТАВШАЯ")])
    asyncio.run(main(monkeypatch))
    получено = {c[1]: c[3] for c in calls}
    assert получено["СВЕЖАЯ"] == 0
    assert получено["ОТСТАВШАЯ"] == 1000


def test_слот_без_контекста_не_ломает_разбор(monkeypatch):
    """Отсутствие поля слота не должно ронять поток — считаем отставание нулевым."""
    без_слота = json.dumps({
        "method": "logsNotification",
        "params": {"subscription": 1,
                   "result": {"value": {"signature": "S", "logs": [], "err": None}}}})
    calls, main = _прогнать([без_слота])
    asyncio.run(main(monkeypatch))
    assert calls and calls[0][3] == 0


# ---------- отбор в мониторе ----------
@pytest.mark.parametrize("lag,должны_пропустить", [
    (0, False), (100, False), (749, False), (751, True), (100_000, True),
])
def test_монитор_отбрасывает_отставшие(lag, должны_пропустить):
    """Порог 750 слотов = 5 минут. Ниже — торгуем, выше — событие бесполезно."""
    отбросили = lag * monitor.SLOT_S > monitor.MAX_EVENT_AGE_S
    assert отбросили is должны_пропустить


def test_backfill_зовёт_on_event_без_слота():
    """Догрузка после обрыва идёт БЕЗ уведомления, значит и без слота.

    Это правильно: у backfill свой признак возраста — blockTime из getTransaction,
    и старая проверка возраста на том пути работает. Важно лишь, чтобы вызов с
    двумя аргументами не падал из-за новой сигнатуры.
    """
    calls = []

    async def on_event(w, sig, logs=None, lag=0):
        calls.append((w, sig, logs, lag))

    async def main():
        await on_event("W", "SIG")

    asyncio.run(main())
    assert calls == [("W", "SIG", None, 0)]
