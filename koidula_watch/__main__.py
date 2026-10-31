"""Точка входа: python -m koidula_watch <команда>."""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import sys
from datetime import datetime

from . import __version__
from .config import Config, ConfigError, load_config
from .fixtures import scenario_html
from .goswift import DayResult, GoSwiftClient, GoSwiftError, evaluate_day, parse_slots
from .monitor import Monitor
from .notifier import ConsoleNotifier, NotifyError, TelegramNotifier
from .state import CheckJournal, StateStore

log = logging.getLogger("koidula_watch")


def setup_logging(config: Config, to_file: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Время в логах — Europe/Tallinn, независимо от часового пояса сервера.
    fmt.converter = lambda ts: datetime.fromtimestamp(ts, config.tz).timetuple()
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.handlers = [stream]
    if to_file:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            config.data_dir / "monitor.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(fmt)
        root.addHandler(handler)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def make_client(config: Config) -> GoSwiftClient:
    return GoSwiftClient(
        base_url=config.base_url,
        vehicle_category=config.vehicle_category,
        border_point_id=config.border_point_id,
        queue_type=config.queue_type,
        timeout=config.request_timeout,
        user_agent=config.user_agent,
    )


def resolve_chat_id(config: Config) -> str:
    """TELEGRAM_CHAT_ID из окружения; если пуст — из data/chat_id или из /start, присланного боту.

    Сам chat_id в лог не пишется (логи GitHub Actions публичны для публичного репозитория).
    """
    if config.telegram_chat_id:
        return config.telegram_chat_id
    cache = config.data_dir / "chat_id"
    if cache.exists() and cache.read_text().strip():
        return cache.read_text().strip()
    if not config.telegram_bot_token:
        raise ConfigError("Не задан TELEGRAM_BOT_TOKEN")
    try:
        updates = TelegramNotifier(config.telegram_bot_token, "").get_updates()
    except NotifyError as exc:
        raise ConfigError(f"Не удалось прочитать сообщения боту: {exc}") from exc
    private = {
        str(m["chat"]["id"])
        for u in updates
        if (m := u.get("message")) and m.get("chat", {}).get("type") == "private"
    }
    if len(private) != 1:
        raise ConfigError(
            "TELEGRAM_CHAT_ID не задан, и определить его автоматически не удалось "
            f"(личных чатов с ботом за последние 24 ч: {len(private)}). "
            "Отправьте боту /start или задайте секрет TELEGRAM_CHAT_ID."
        )
    chat_id = private.pop()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    cache.write_text(chat_id)
    log.info("chat_id определён автоматически по сообщению /start (значение не выводится)")
    return chat_id


def make_telegram(config: Config) -> TelegramNotifier:
    if not config.telegram_bot_token:
        config.require_telegram()
    return TelegramNotifier(config.telegram_bot_token, resolve_chat_id(config))


def describe(result: DayResult) -> str:
    lines = [f"Дата: {result.target_date:%d.%m.%Y}", f"Статус: {result.status.value}",
             f"Счётчики: {result.counts()}"]
    for slot in result.slots:
        lines.append(f"  {slot.start:%H:%M}  {slot.state.value:<9} «{slot.text}»")
    return "\n".join(lines)


# ── команды ────────────────────────────────────────────────────────────────
def cmd_run(config: Config, args) -> int:
    setup_logging(config)
    monitor = Monitor(config, make_client(config), make_telegram(config),
                      StateStore(config.data_dir / "state.json"), CheckJournal(config.data_dir / "checks.jsonl"))
    log.info("koidula_watch %s: %s, %s, дата %s", __version__, config.border_point_name,
             config.vehicle_category, config.target_date)
    monitor.run_forever(ignore_start=args.now)
    return 0


def cmd_check_once(config: Config, args) -> int:
    """Одна реальная проверка сайта. Telegram — только с --notify."""
    setup_logging(config, to_file=False)
    client = make_client(config)
    try:
        result = client.check_day(config.target_date)
    except GoSwiftError as exc:
        print(f"ОШИБКА: {type(exc).__name__}: {exc}")
        return 2
    print(describe(result))
    if args.notify:
        notifier = make_telegram(config)
        notifier.send(f"🔎 Ручная проверка {config.target_date:%d.%m.%Y}: <b>{result.status.value}</b>, "
                      f"свободно: {', '.join(result.free_hours) or 'нет'}")
    return 0


def cmd_test_telegram(config: Config, args) -> int:
    setup_logging(config, to_file=False)
    notifier = make_telegram(config)
    try:
        me = notifier.get_me()
        print(f"Бот: @{me.get('username')} (id {me.get('id')})")
        notifier.send(
            "🧪 <b>[ТЕСТ] Проверка доставки уведомлений</b>\n"
            f"Монитор {config.border_point_name}, категория {config.vehicle_category}, дата "
            f"{config.target_date:%d.%m.%Y}.\nЭто тестовое сообщение — оно <b>не</b> означает наличие мест.\n"
            f"Время: {datetime.now(config.tz):%d.%m.%Y %H:%M:%S} ({config.tz.key})"
        )
    except NotifyError as exc:
        print(f"ОШИБКА: {exc}")
        return 2
    print("Тестовое сообщение отправлено.")
    return 0


class _FixtureChecker:
    def __init__(self, html: str | None, error: Exception | None = None):
        self.html, self.error = html, error

    def check_day(self, target):
        if self.error:
            raise self.error
        return evaluate_day(parse_slots(self.html), target)  # реальный парсер


def cmd_simulate(config: Config, args) -> int:
    """Прогон всего конвейера на смоделированном ответе сайта. Сайт не запрашивается."""
    setup_logging(config, to_file=False)
    notifier = ConsoleNotifier() if args.dry else make_telegram(config)
    state_path = config.data_dir / "simulate-state.json"
    state_path.unlink(missing_ok=True)  # тест не влияет на дедупликацию боевого режима
    store = StateStore(state_path)
    journal = CheckJournal(config.data_dir / "simulate-checks.jsonl")

    def monitor(checker) -> Monitor:
        return Monitor(config, checker, notifier, store, journal, sleep_fn=lambda s: None,
                       label="🧪 [ТЕСТ, данные смоделированы] ")

    if args.scenario == "error":
        failing = monitor(_FixtureChecker(None, GoSwiftError("смоделированный сбой сети")))
        for _ in range(config.error_alert_threshold):
            failing.check_once()
        print(f"После {config.error_alert_threshold} ошибок подряд должно прийти сообщение об ошибке.")
        monitor(_FixtureChecker(scenario_html("not_open", config.target_date))).check_once()
        print("Затем успешная проверка — сообщение о восстановлении.")
        return 0

    html = scenario_html(args.scenario, config.target_date)
    first = monitor(_FixtureChecker(html))
    first.check_once()
    print(f"Проверка 1: статус {first.state.last_status}, свободно: {first.state.last_free or 'нет'}")
    second = monitor(_FixtureChecker(html))
    second.check_once()
    print("Проверка 2 с теми же данными: повторное уведомление должно быть подавлено.")
    return 0


def cmd_get_chat_id(config: Config, args) -> int:
    if not config.telegram_bot_token:
        print("Задайте TELEGRAM_BOT_TOKEN.")
        return 2
    notifier = TelegramNotifier(config.telegram_bot_token, "")
    try:
        updates = notifier.get_updates()
    except NotifyError as exc:
        print(f"ОШИБКА: {exc}")
        return 2
    chats = {}
    for update in updates:
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name")
    if not chats:
        print("Нет сообщений. Напишите боту /start в Telegram и повторите команду.")
        return 1
    for chat_id, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={chat_id}   # {name}")
    return 0


def cmd_healthcheck(config: Config, args) -> int:
    path = config.data_dir / "health.json"
    try:
        ts = datetime.fromisoformat(json.loads(path.read_text())["ts"])
    except (OSError, ValueError, KeyError):
        return 1
    return 0 if (datetime.now(config.tz) - ts).total_seconds() < 15 * 60 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="koidula_watch", description="Мониторинг слотов GoSwift на одну дату")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="основной режим по расписанию (по умолчанию)")
    run.add_argument("--now", action="store_true", help="начать проверки сразу, не дожидаясь MONITOR_START")

    once = sub.add_parser("check-once", help="одна реальная проверка сайта с выводом всех 24 слотов")
    once.add_argument("--notify", action="store_true", help="также отправить результат в Telegram")

    sub.add_parser("test-telegram", help="отправить тестовое сообщение в Telegram")

    sim = sub.add_parser("simulate", help="прогнать уведомления на смоделированных данных")
    sim.add_argument("scenario", choices=["free", "full", "not_open", "other_day_free", "error"])
    sim.add_argument("--dry", action="store_true", help="печатать сообщения, а не отправлять в Telegram")

    sub.add_parser("get-chat-id", help="показать chat_id из последних сообщений боту")
    sub.add_parser("healthcheck", help="код 0, если цикл жив (для Docker HEALTHCHECK)")

    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(["run", *(argv or [])])
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2
    handlers = {"run": cmd_run, "check-once": cmd_check_once, "test-telegram": cmd_test_telegram,
                "simulate": cmd_simulate, "get-chat-id": cmd_get_chat_id, "healthcheck": cmd_healthcheck}
    try:
        return handlers[args.command](config, args)
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
