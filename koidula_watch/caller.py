"""Голосовой звонок в Telegram через сторонний сервис CallMeBot (api.callmebot.com).

Бот Telegram сам звонить не может. CallMeBot звонит пользователю голосовым вызовом
Telegram и зачитывает текст. Каждый получатель должен один раз отправить /start
боту @CallMeBot_txtbot. Список получателей — переменная CALLMEBOT_USERS
(@username или +номер через запятую). Сбой звонка не мешает Telegram-уведомлениям.
"""

from __future__ import annotations

import logging
import os
import threading

import requests

log = logging.getLogger(__name__)

API = "https://api.callmebot.com/start.php"


def recipients() -> list[str]:
    # Отключено по умолчанию: 31.10.2026 сервис потребовал платный контакт через канал (650 звёзд).
    if os.environ.get("CALLMEBOT_ENABLED", "false").lower() not in {"1", "true", "yes"}:
        return []
    raw = os.environ.get("CALLMEBOT_USERS", "")
    return [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]


def _call_one(user: str, text: str, timeout: float) -> tuple[str, bool, str]:
    try:
        response = requests.get(
            API,
            params={"user": user, "text": text[:250], "lang": "ru-RU-Standard-A", "rpt": "3", "cc": "missed"},
            timeout=timeout,
        )
        body = response.text[:300].replace("\n", " ")
        ok = response.status_code == 200 and "error" not in body.lower()
        return user, ok, body
    except requests.RequestException as exc:
        return user, False, str(exc)


def call_all(text: str, timeout: float = 60.0, wait: bool = False) -> list[tuple[str, bool, str]]:
    """Звонит всем получателям. По умолчанию — в фоне, чтобы не задерживать проверки."""
    users = recipients()
    if not users:
        return []
    results: list[tuple[str, bool, str]] = []

    def worker() -> None:
        for user in users:  # последовательно: CallMeBot обслуживает звонки по очереди
            res = _call_one(user, text, timeout)
            results.append(res)
            log.info("CallMeBot звонок #%d: %s", users.index(user) + 1, "успешно" if res[1] else f"ошибка: {res[2]}")

    if wait:
        worker()
    else:
        threading.Thread(target=worker, daemon=False).start()
    return results
