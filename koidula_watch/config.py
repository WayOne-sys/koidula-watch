"""Конфигурация из переменных окружения.

Секреты (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) читаются только из окружения
и никогда не логируются.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Нижняя граница интервала проверки. Сайт не публикует лимитов частоты,
# поэтому монитор не опускается ниже этого значения даже по настройке.
HARD_MIN_INTERVAL_SECONDS = 20

# Идентификаторы, найденные в публичном мастере бронирования (шаги 1–3).
BORDER_POINTS = {"narva": "1", "koidula": "2", "luhamaa": "3"}
QUEUE_TYPE_CERTAIN_TIME = "1"  # «Reserve for certain time»


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class IntensiveWindow:
    start: datetime
    end: datetime
    interval_seconds: int

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end


@dataclass(frozen=True)
class Config:
    tz: ZoneInfo
    target_date: date
    border_point_id: str
    border_point_name: str
    vehicle_category: str
    queue_type: str
    monitor_start: datetime
    monitor_stop: datetime
    check_interval_seconds: int
    intensive_windows: tuple[IntensiveWindow, ...]
    min_interval_seconds: int
    jitter_fraction: float
    error_alert_threshold: int
    error_alert_repeat_minutes: int
    repeat_cooldown_minutes: int
    notify_opened_full: bool
    heartbeat_hour: int | None
    stop_on_available: bool
    max_runtime_minutes: int
    exit_when_done: bool
    data_dir: Path
    base_url: str
    request_timeout: float
    user_agent: str
    telegram_bot_token: str = field(repr=False, default="")
    telegram_chat_id: str = field(repr=False, default="")

    @property
    def booking_url(self) -> str:
        return f"{self.base_url}/yphis/preReserveSelectVehicle.action"

    def require_telegram(self) -> None:
        missing = [
            name
            for name, value in (
                ("TELEGRAM_BOT_TOKEN", self.telegram_bot_token),
                ("TELEGRAM_CHAT_ID", self.telegram_chat_id),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "Не заданы переменные окружения: " + ", ".join(missing)
                + ". Скопируйте .env.example в .env и заполните их."
            )


def _env(env: dict[str, str], name: str, default: str) -> str:
    value = env.get(name, "")
    return value.strip() if value and value.strip() else default


def _parse_local(value: str, tz: ZoneInfo, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError(f"{name}: ожидается формат YYYY-MM-DDTHH:MM, получено {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _parse_int(value: str, name: str, minimum: int | None = None) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name}: ожидается целое число, получено {value!r}") from exc
    if minimum is not None and number < minimum:
        raise ConfigError(f"{name}: значение должно быть не меньше {minimum}")
    return number


def _parse_bool(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on", "да"}


def parse_windows(raw: str, tz: ZoneInfo) -> tuple[IntensiveWindow, ...]:
    """Формат: START/END@SECONDS, несколько окон через запятую.

    Пример: 2026-10-31T23:30/2026-11-01T03:00@30
    """
    windows: list[IntensiveWindow] = []
    for chunk in filter(None, (part.strip() for part in raw.split(","))):
        try:
            span, seconds = chunk.rsplit("@", 1)
            start_raw, end_raw = span.split("/", 1)
        except ValueError as exc:
            raise ConfigError(
                f"INTENSIVE_WINDOWS: неверный фрагмент {chunk!r}, нужен START/END@SECONDS"
            ) from exc
        start = _parse_local(start_raw, tz, "INTENSIVE_WINDOWS")
        end = _parse_local(end_raw, tz, "INTENSIVE_WINDOWS")
        if end <= start:
            raise ConfigError(f"INTENSIVE_WINDOWS: конец раньше начала в {chunk!r}")
        windows.append(IntensiveWindow(start, end, _parse_int(seconds, "INTENSIVE_WINDOWS", 1)))
    return tuple(sorted(windows, key=lambda w: w.start))


def load_config(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ) if env is None else env

    tz_name = _env(env, "TZ_NAME", "Europe/Tallinn")
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:  # noqa: BLE001 - любой сбой загрузки зоны
        raise ConfigError(f"TZ_NAME: неизвестный часовой пояс {tz_name!r}") from exc

    try:
        target_date = date.fromisoformat(_env(env, "TARGET_DATE", "2026-12-20"))
    except ValueError as exc:
        raise ConfigError("TARGET_DATE: ожидается формат YYYY-MM-DD") from exc

    point_name = _env(env, "BORDER_POINT", "koidula").lower()
    if point_name not in BORDER_POINTS:
        raise ConfigError(f"BORDER_POINT: допустимо {', '.join(BORDER_POINTS)}")

    min_interval = max(
        HARD_MIN_INTERVAL_SECONDS,
        _parse_int(_env(env, "MIN_INTERVAL_SECONDS", str(HARD_MIN_INTERVAL_SECONDS)), "MIN_INTERVAL_SECONDS", 1),
    )

    heartbeat_raw = _env(env, "HEARTBEAT_HOUR", "9")
    heartbeat_hour = None if heartbeat_raw.lower() in {"off", "none", "-1"} else _parse_int(heartbeat_raw, "HEARTBEAT_HOUR", 0)
    if heartbeat_hour is not None and heartbeat_hour > 23:
        raise ConfigError("HEARTBEAT_HOUR: 0–23 или off")

    jitter = float(_env(env, "JITTER_FRACTION", "0.05"))
    if not 0 <= jitter < 0.5:
        raise ConfigError("JITTER_FRACTION: от 0 до 0.5")

    config = Config(
        tz=tz,
        target_date=target_date,
        border_point_id=BORDER_POINTS[point_name],
        border_point_name=point_name.capitalize(),
        vehicle_category=_env(env, "VEHICLE_CATEGORY", "B").upper(),
        queue_type=QUEUE_TYPE_CERTAIN_TIME,
        monitor_start=_parse_local(_env(env, "MONITOR_START", "2026-10-31T23:00"), tz, "MONITOR_START"),
        monitor_stop=_parse_local(_env(env, "MONITOR_STOP", "2026-12-20T19:00"), tz, "MONITOR_STOP"),
        check_interval_seconds=_parse_int(_env(env, "CHECK_INTERVAL_SECONDS", "300"), "CHECK_INTERVAL_SECONDS", 1),
        intensive_windows=parse_windows(
            _env(
                env,
                "INTENSIVE_WINDOWS",
                "2026-10-31T23:00/2026-11-01T00:00@900,2026-11-01T00:00/2026-11-01T00:30@60",
            ),
            tz,
        ),
        min_interval_seconds=min_interval,
        jitter_fraction=jitter,
        error_alert_threshold=_parse_int(_env(env, "ERROR_ALERT_THRESHOLD", "5"), "ERROR_ALERT_THRESHOLD", 1),
        error_alert_repeat_minutes=_parse_int(_env(env, "ERROR_ALERT_REPEAT_MINUTES", "30"), "ERROR_ALERT_REPEAT_MINUTES", 1),
        repeat_cooldown_minutes=_parse_int(_env(env, "REPEAT_COOLDOWN_MINUTES", "15"), "REPEAT_COOLDOWN_MINUTES", 0),
        notify_opened_full=_parse_bool(_env(env, "NOTIFY_OPENED_FULL", "true")),
        heartbeat_hour=heartbeat_hour,
        stop_on_available=_parse_bool(_env(env, "STOP_ON_AVAILABLE", "true")),
        max_runtime_minutes=_parse_int(_env(env, "MAX_RUNTIME_MINUTES", "0"), "MAX_RUNTIME_MINUTES", 0),
        exit_when_done=_parse_bool(_env(env, "EXIT_WHEN_DONE", "false")),
        data_dir=Path(_env(env, "DATA_DIR", "./data")),
        base_url=_env(env, "BASE_URL", "https://www.estonianborder.eu").rstrip("/"),
        request_timeout=float(_env(env, "REQUEST_TIMEOUT_SECONDS", "20")),
        user_agent=_env(
            env,
            "USER_AGENT",
            "KoidulaQueueWatch/1.0 (personal availability monitor; max 1 request/min; no booking)",
        ),
        telegram_bot_token=env.get("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=env.get("TELEGRAM_CHAT_ID", "").strip(),
    )
    if config.monitor_stop <= config.monitor_start:
        raise ConfigError("MONITOR_STOP должен быть позже MONITOR_START")
    return config
