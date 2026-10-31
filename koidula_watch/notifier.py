"""Отправка уведомлений через Telegram Bot API."""

from __future__ import annotations

import html
import logging
import time

import requests

log = logging.getLogger(__name__)


class NotifyError(Exception):
    pass


def esc(text: object) -> str:
    return html.escape(str(text), quote=False)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, timeout: float = 15.0, session: requests.Session | None = None):
        self._token = token
        self.chat_id = chat_id
        self.timeout = timeout
        self._http = session or requests.Session()

    def _api(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self._token}/{method}"

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "<TOKEN>") if self._token else text

    def send(self, text: str, attempts: int = 3) -> None:
        """Отправляет HTML-сообщение. Повторяет при сетевых ошибках и 429."""
        last_error = ""
        for attempt in range(1, attempts + 1):
            try:
                response = self._http.post(
                    self._api("sendMessage"),
                    json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                          "disable_web_page_preview": True},
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = self._redact(str(exc))
                log.warning("Telegram: сетевая ошибка (попытка %d/%d): %s", attempt, attempts, last_error)
                time.sleep(min(2 ** attempt, 10))
                continue
            if response.status_code == 200:
                return
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            description = payload.get("description", response.text[:200])
            last_error = f"HTTP {response.status_code}: {description}"
            if response.status_code == 429:
                delay = payload.get("parameters", {}).get("retry_after", 5)
                log.warning("Telegram: лимит, жду %s с", delay)
                time.sleep(min(float(delay), 60))
                continue
            if response.status_code in (400, 401, 403, 404):
                break  # повтор не поможет: неверный токен/chat_id или бот заблокирован
            time.sleep(min(2 ** attempt, 10))
        raise NotifyError(f"Не удалось отправить сообщение в Telegram: {last_error}")

    def get_updates(self) -> list[dict]:
        response = self._http.get(self._api("getUpdates"), timeout=self.timeout)
        if response.status_code != 200:
            raise NotifyError(f"getUpdates: HTTP {response.status_code}: {response.text[:200]}")
        return response.json().get("result", [])

    def get_me(self) -> dict:
        response = self._http.get(self._api("getMe"), timeout=self.timeout)
        if response.status_code != 200:
            raise NotifyError(f"getMe: HTTP {response.status_code}: {response.text[:200]}")
        return response.json().get("result", {})


class ConsoleNotifier:
    """Печатает сообщения в консоль (для check-once и сухих прогонов)."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str, attempts: int = 1) -> None:
        self.sent.append(text)
        print("── сообщение (не отправлено в Telegram) ──\n" + text + "\n──")
