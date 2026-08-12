"""Управление ботом из Telegram: четыре безопасные команды.

/статус  — режим, риск, открытые позиции, версия конфига
/баланс  — состояние кошелька
/стоп    — прекратить открывать НОВЫЕ позиции (уже открытые доводятся до выхода)
/старт   — снова разрешить входы

ЧЕГО ЗДЕСЬ НЕТ И НЕ БУДЕТ. Ни вывода средств, ни смены размера клипа, ни включения
живого режима. Всё, что двигает деньги, требует доступа к серверу — переписка в
мессенджере не тот канал, где это уместно.

БЕЗОПАСНОСТЬ. Отвечаем ТОЛЬКО на сообщения из чата, указанного в TELEGRAM_CHAT_ID.
Чужой, узнавший имя бота, может слать ему что угодно — команды от него молча
игнорируются. Токен и ключи не печатаются никогда.

ОСТАНОВКА ПЕРЕЖИВАЕТ ПЕРЕЗАПУСК. Флаг лежит в файле, а не только в памяти: иначе
владелец останавливает торговлю, контейнер перезапускается по любой причине — и
входы молча возобновляются. Это ровно тот класс тихого сюрприза, которого быть
не должно.
"""
from __future__ import annotations

import json
import time

import requests

from . import config

ФАЙЛ_ПАУЗЫ = "trading_paused.json"
ОПРОС_С = 25          # длинный опрос: одно соединение вместо частых запросов
АЛИАСЫ = {
    "/статус": "статус", "/status": "статус",
    "/баланс": "баланс", "/balance": "баланс",
    "/стоп": "стоп", "/stop": "стоп",
    "/старт": "старт", "/start": "старт",
}


def на_паузе() -> bool:
    try:
        with open(config.OUTPUT_DIR / ФАЙЛ_ПАУЗЫ, encoding="utf-8") as f:
            return bool(json.load(f).get("paused"))
    except Exception:  # noqa: BLE001 — нет файла или он битый = торгуем
        return False


def поставить_паузу(значение: bool, кем: str = "telegram") -> None:
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.OUTPUT_DIR / (ФАЙЛ_ПАУЗЫ + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"paused": bool(значение), "ts": time.time(), "кем": кем}, f,
                  ensure_ascii=False)
    tmp.replace(config.OUTPUT_DIR / ФАЙЛ_ПАУЗЫ)


def _разобрать(update: dict, свой_чат: str) -> str | None:
    """→ имя команды или None. Чужой чат отсекается здесь и только здесь."""
    msg = update.get("message") or update.get("edited_message") or {}
    чат = str(((msg.get("chat") or {}).get("id")) or "")
    if not чат or чат != str(свой_чат):
        return None
    текст = (msg.get("text") or "").strip().split("@")[0].lower()
    return АЛИАСЫ.get(текст)


def клавиатура() -> dict:
    """Кнопки под полем ввода — чтобы не набирать команды руками."""
    return {"keyboard": [[{"text": "/статус"}, {"text": "/баланс"}],
                         [{"text": "/стоп"}, {"text": "/старт"}]],
            "resize_keyboard": True}


def _послать(token: str, chat: str, текст: str) -> None:
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": текст, "reply_markup": клавиатура(),
                            "disable_web_page_preview": True}, timeout=15)
    except Exception as e:  # noqa: BLE001
        print(f"[команды] отправка не удалась: {type(e).__name__}")


def выполнить(команда: str, обработчики: dict) -> str:
    """Чистая функция: команда + словарь обработчиков → текст ответа."""
    if команда == "стоп":
        поставить_паузу(True)
        return ("🛑 ВХОДЫ ОСТАНОВЛЕНЫ. Открытые позиции доводятся до выхода штатно — "
                "бросать их на полпути опаснее, чем закрыть по правилам.\n"
                "Возобновить: /старт")
    if команда == "старт":
        поставить_паузу(False)
        return "✅ Входы разрешены."
    fn = обработчики.get(команда)
    if not fn:
        return "неизвестная команда"
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — ответ на команду не имеет права ронять монитор
        return f"не удалось собрать ответ: {type(e).__name__}"


def опросить(обработчики: dict, offset: int | None = None,
             таймаут: int = ОПРОС_С) -> tuple[int | None, bool]:
    """Один цикл длинного опроса. → (новый offset, настроен ли Telegram).

    ДВА ЗНАЧЕНИЯ, А НЕ ОДНО. В первой версии возвращался только offset, и `None`
    означал сразу и «Telegram не настроен», и «обновлений не было». Петля команд
    гасила себя на первом же пустом опросе — то есть кнопки переставали работать
    навсегда, молча. Поймано при проверке на боевом сервере.
    """
    token = config.secret("TELEGRAM_BOT_TOKEN", required=False)
    chat = config.secret("TELEGRAM_CHAT_ID", required=False)
    if not token or not chat:
        return None, False
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params={"timeout": таймаут, "offset": offset},
                         timeout=таймаут + 10)
        данные = r.json()
    except Exception as e:  # noqa: BLE001
        print(f"[команды] опрос не удался: {type(e).__name__}")
        return offset, True
    for upd in данные.get("result") or []:
        offset = upd.get("update_id", 0) + 1
        команда = _разобрать(upd, chat)
        if not команда:
            continue
        print(f"[команда] {команда}", flush=True)
        _послать(token, chat, выполнить(команда, обработчики))
    return offset, True
