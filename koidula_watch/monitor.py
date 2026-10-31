"""Цикл мониторинга: расписание, оценка результата, уведомления, дедупликация."""

from __future__ import annotations

import json
import logging
import os
import random
import time
from datetime import datetime, timedelta
from typing import Callable, Protocol

import requests

from .config import Config
from .goswift import DayResult, DayStatus, GoSwiftError, RateLimitedError, TargetDateMissingError
from . import caller
from .notifier import NotifyError, esc
from .state import CheckJournal, State, StateStore

log = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 900
FMT = "%d.%m.%Y %H:%M:%S"


def gha_notice(text: str) -> None:
    """Аннотация GitHub Actions (видна в интерфейсе и через API, без скачивания логов)."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("::notice title=koidula::" + text.replace("%", "%25").replace("\n", "%0A"), flush=True)


class Notifier(Protocol):
    def send(self, text: str, attempts: int = 3) -> None: ...


class Checker(Protocol):
    def check_day(self, target) -> DayResult: ...


class Schedule:
    def __init__(self, config: Config, rng: random.Random | None = None):
        self.c = config
        self.rng = rng or random.Random()

    def phase(self, now: datetime) -> str:
        if now < self.c.monitor_start:
            return "before"
        if now >= self.c.monitor_stop:
            return "after"
        return "active"

    def base_interval(self, now: datetime) -> int:
        for window in self.c.intensive_windows:
            if window.contains(now):
                return max(self.c.min_interval_seconds, window.interval_seconds)
        return max(self.c.min_interval_seconds, self.c.check_interval_seconds)

    def in_intensive(self, now: datetime) -> bool:
        return any(w.contains(now) for w in self.c.intensive_windows)

    def next_delay(self, now: datetime) -> float:
        base = self.base_interval(now)
        delay = base * (1 + self.rng.uniform(0.0, self.c.jitter_fraction))  # только вверх: интервал не короче заданного
        delay = max(float(self.c.min_interval_seconds), delay)
        # Не «проспать» начало ближайшего усиленного окна.
        for window in self.c.intensive_windows:
            if now < window.start < now + timedelta(seconds=delay):
                delay = max(1.0, (window.start - now).total_seconds())
        return delay


class Monitor:
    def __init__(
        self,
        config: Config,
        checker: Checker,
        notifier: Notifier,
        store: StateStore,
        journal: CheckJournal,
        now_fn: Callable[[], datetime] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        label: str = "",
    ):
        self.c = config
        self.checker = checker
        self.notifier = notifier
        self.store = store
        self.journal = journal
        self.now = now_fn or (lambda: datetime.now(config.tz))
        self.sleep = sleep_fn
        self.label = label  # например "[ТЕСТ] "
        self.schedule = Schedule(config)
        self.state: State = store.load()

    # ── оформление ──────────────────────────────────────────────────────────
    @property
    def _target(self) -> str:
        return self.c.target_date.strftime("%d.%m.%Y")

    @property
    def _route(self) -> str:
        return f"{self.c.border_point_name}, категория {self.c.vehicle_category}, Эстония → Россия"

    def _stamp(self, now: datetime) -> str:
        return f"{now.strftime(FMT)} ({self.c.tz.key})"

    def _send(self, text: str) -> bool:
        try:
            self.notifier.send(self.label + text)
            return True
        except NotifyError as exc:
            log.error("%s", exc)
            return False

    # ── закреплённое сообщение со статусом ──────────────────────────────────
    _STATUS_RU = {
        "not_open": "бронирование ещё не открыто",
        "opened_full": "открыто, но все места заняты (Full)",
        "available": "ЕСТЬ СВОБОДНЫЕ МЕСТА",
        "unclear": "непонятное состояние — проверьте вручную",
    }

    def _status_text(self, now: datetime, phase: str, next_at: datetime | None = None) -> str:
        st = self.state
        if st.stopped:
            head = f"⏹ <b>Остановлен</b>: {esc(st.stop_reason)}"
        elif phase == "waiting":
            head = f"⏳ <b>Ожидает старта</b> {self.c.monitor_start:%d.%m %H:%M}"
        elif st.consecutive_errors:
            head = f"🟠 <b>Ошибки</b>: {st.consecutive_errors} подряд (повторяю с паузой)"
        else:
            head = "🟢 <b>Активен</b>"
        last = datetime.fromisoformat(st.last_check_at).strftime("%d.%m %H:%M:%S") if st.last_check_at else "—"
        day = self._STATUS_RU.get(st.last_status, st.last_status or "ещё не проверялось")
        if st.last_status == "available" and st.last_free:
            day += ": " + ", ".join(st.last_free)
        lines = [
            "📊 <b>Статус монитора</b> (обновляется автоматически)",
            head,
            f"Последняя проверка: <b>{last}</b>",
            f"{self._target} Koidula B: {esc(day)}",
        ]
        if next_at and not st.stopped:
            lines.append(f"Следующая: ~{next_at:%H:%M:%S}")
        lines.append(f"Проверок всего: {st.checks_total}")
        lines.append("<i>Если время последней проверки отстаёт больше чем на 10–15 мин — монитор не работает.</i>")
        return "\n".join(lines)

    def update_status(self, phase: str = "active", next_at: datetime | None = None) -> None:
        notifier = self.notifier
        if not hasattr(notifier, "edit") or self.label:
            return
        text = self._status_text(self.now(), phase, next_at)
        try:
            if self.state.status_message_id:
                notifier.edit(self.state.status_message_id, text)
                return
        except NotifyError as exc:
            log.warning("Статус: не удалось отредактировать (%s) — создаю новое сообщение", exc)
        try:
            sender = getattr(notifier, "send_primary", notifier.send)
            message_id = sender(text)
            if message_id:
                self.state.status_message_id = int(message_id)
                try:
                    notifier.pin(message_id)
                except NotifyError as exc:
                    log.warning("Статус: не удалось закрепить: %s", exc)
            self.store.save(self.state)
        except NotifyError as exc:
            log.warning("Статус: не удалось отправить: %s", exc)

    # ── одна проверка ───────────────────────────────────────────────────────
    def check_once(self) -> float:
        """Выполняет одну проверку и возвращает рекомендуемую паузу до следующей (сек)."""
        now = self.now()
        record: dict = {"ts": now.isoformat(timespec="seconds"), "target": self.c.target_date.isoformat(),
                        "intensive": self.schedule.in_intensive(now)}
        delay = self.schedule.next_delay(now)
        try:
            result = self.checker.check_day(self.c.target_date)
        except RateLimitedError as exc:
            delay = self._on_error(now, exc, record)
            if exc.retry_after:
                delay = max(delay, exc.retry_after)
        except (GoSwiftError, requests.RequestException) as exc:
            delay = self._on_error(now, exc, record)
        else:
            self._on_success(now, result, record)
        self.state.checks_total += 1
        self.state.last_check_at = record["ts"]
        record["next_in_s"] = round(delay, 1)
        self.store.save(self.state)
        self.journal.write(record)
        return delay

    def _on_error(self, now: datetime, exc: Exception, record: dict) -> float:
        self.state.consecutive_errors += 1
        n = self.state.consecutive_errors
        kind = "нет целевой даты" if isinstance(exc, TargetDateMissingError) else type(exc).__name__
        record.update(ok=False, error=f"{kind}: {exc}", consecutive_errors=n)
        log.warning("Проверка #%d неудачна (%d подряд): %s", self.state.checks_total + 1, n, exc)

        if n in (1, self.c.error_alert_threshold):
            gha_notice(f"{now:%d.%m %H:%M:%S}: ошибка #{n}: {kind}: {exc}")
        due = n >= self.c.error_alert_threshold
        if due and self.state.error_alert_active and self.state.last_error_alert_at:
            last = datetime.fromisoformat(self.state.last_error_alert_at)
            due = now - last >= timedelta(minutes=self.c.error_alert_repeat_minutes)
        if due:
            sent = self._send(
                f"🔴 <b>Ошибка мониторинга</b>\n{esc(self._route)}\n"
                f"{n} проверок подряд не удались. Последняя ошибка:\n<code>{esc(kind)}: {esc(exc)}</code>\n\n"
                f"Пока ошибка не устранена, доступность на {self._target} <b>не проверяется</b>.\n"
                f"Время: {self._stamp(now)}"
            )
            if sent:
                self.state.error_alert_active = True
                self.state.last_error_alert_at = now.isoformat()
        base = self.schedule.base_interval(now)
        return float(min(MAX_BACKOFF_SECONDS, base * 2 ** min(n - 1, 5)))

    def _on_success(self, now: datetime, result: DayResult, record: dict) -> None:
        if self.state.error_alert_active:
            self._send(f"✅ Мониторинг восстановлен после {self.state.consecutive_errors} ошибок. "
                       f"Время: {self._stamp(now)}")
        self.state.consecutive_errors = 0
        self.state.error_alert_active = False

        free = result.free_hours
        if result.status.value != self.state.last_status or self.state.checks_total == 0:
            gha_notice(f"{now:%d.%m %H:%M:%S}: {self._target} -> {result.status.value} {result.counts()} free={free}")
        record.update(ok=True, status=result.status.value, counts=result.counts(), free=free)
        log.info("Проверка: %s %s → %s %s", self._target, self.c.border_point_name, result.status.value,
                 result.counts())

        delivered = True
        if result.status == DayStatus.AVAILABLE:
            delivered = self._handle_available(now, free, record)
        if result.status == DayStatus.OPENED_FULL and self.c.notify_opened_full and not self.state.opened_full_notified:
            if self._send(
                f"🟡 <b>Календарь на {self._target} открыт, но свободных мест нет</b>\n{esc(self._route)}\n"
                f"Все открытые слоты помечены «Full». Это <b>не</b> доступность бронирования.\n"
                f"Продолжаю проверять: места могут появиться после отмен.\nВремя: {self._stamp(now)}"
            ):
                self.state.opened_full_notified = True
                if not self.label:
                    caller.call_all("Койдула. Бронирование на двадцатое декабря открылось, но все места заняты. "
                                    "Монитор продолжает проверять.")
        if result.status == DayStatus.UNCLEAR:
            if not self.state.unclear_notified and self._send(
                f"⚠️ <b>Непонятное состояние слотов на {self._target}</b>\n"
                f"Сайт вернул слоты без признаков «свободно/занято/не открыто»: {esc(result.counts())}.\n"
                f"Возможно, изменилась вёрстка. Проверьте вручную: {esc(self.c.booking_url)}"
            ):
                self.state.unclear_notified = True
        else:
            self.state.unclear_notified = False

        # Если уведомление о свободных местах не ушло, не запоминаем их как «уже известные»,
        # чтобы на следующей проверке отправить снова.
        self.state.last_free = free if delivered else []
        self.state.last_status = result.status.value

    def _handle_available(self, now: datetime, free: list[str], record: dict) -> bool:
        """Возвращает False, только если уведомление нужно было отправить, но оно не ушло."""
        cooldown = timedelta(minutes=self.c.repeat_cooldown_minutes)
        previously_free = set(self.state.last_free)
        new = []
        for hour in free:
            if hour in previously_free:
                continue  # свободен непрерывно с прошлой проверки — уже сообщали
            last = self.state.notified_free.get(hour)
            if last and now - datetime.fromisoformat(last) < cooldown:
                continue  # «мигание» слота — не спамим
            new.append(hour)
        record["new_free"] = new
        if not new:
            log.info("Свободные слоты без изменений (%s) — повторное уведомление подавлено", ", ".join(free))
            return True
        night = [h for h in free if int(h[:2]) >= 19 or int(h[:2]) < 7]
        night_note = (
            "\nℹ️ Koidula закрыт для пересечения 19:00–07:00; ночные брони обслуживаются с 07:00 "
            "(см. объявление на сайте)." if night and self.c.border_point_name == "Koidula" else ""
        )
        text = (
            f"🟢 <b>Есть свободные слоты на {self._target}!</b>\n{esc(self._route)}\n"
            f"Свободно: <b>{esc(', '.join(free))}</b>"
            + (f" (новые: {esc(', '.join(new))})" if set(new) != set(free) else "")
            + f"\n\nБронировать вручную, слоты быстро разбирают:\n{esc(self.c.booking_url)}\n"
            f"B → Koidula checkpoint → Reserve for certain time → {self._target}"
            f"{night_note}\nПроверено: {self._stamp(now)}"
        )
        if self._send(text):
            for hour in free:
                self.state.notified_free[hour] = now.isoformat()
            record["notified"] = True
            if not self.label:
                caller.call_all(f"Внимание! Койдула. Есть свободные места на двадцатое декабря. "
                                f"Время: {', '.join(free)}. Срочно бронируйте в Гоу Свифт.")
            if self.c.stop_on_available:
                self.state.stopped = True
                self.state.stop_reason = f"найдены свободные слоты {', '.join(free)} ({now:%d.%m %H:%M})"
                self._send("⏹ Проверки остановлены после обнаружения мест (STOP_ON_AVAILABLE=true). "
                           "Бронируйте вручную по ссылке выше.")
            return True
        record["notified"] = False
        return False

    # ── служебные сообщения ─────────────────────────────────────────────────
    def heartbeat_if_due(self, now: datetime) -> None:
        if self.c.heartbeat_hour is None or now.hour != self.c.heartbeat_hour:
            return
        today = now.date().isoformat()
        if self.state.last_heartbeat_date == today:
            return
        status = self.state.last_status or "ещё не проверялось"
        if self._send(
            f"💓 Монитор работает. {esc(self._route)}, дата {self._target}.\n"
            f"Проверок всего: {self.state.checks_total}; последний статус: {esc(status)}; "
            f"ошибок подряд: {self.state.consecutive_errors}.\nВремя: {self._stamp(now)}"
        ):
            self.state.last_heartbeat_date = today
            self.store.save(self.state)

    def _touch_health(self, phase: str) -> None:
        path = self.c.data_dir / "health.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ts": self.now().isoformat(timespec="seconds"), "phase": phase}))

    def _sleep_until(self, moment: datetime) -> None:
        while (remaining := (moment - self.now()).total_seconds()) > 0:
            self._touch_health("waiting")
            self.sleep(min(60.0, remaining))

    def _idle_forever(self) -> None:
        # Не выходим: при restart: unless-stopped выход привёл бы к перезапуску и повтору сообщений.
        while True:
            self._touch_health("finished")
            self.sleep(3600)

    def _finish(self) -> None:
        if self.c.exit_when_done:
            return
        self._idle_forever()

    def run_forever(self, ignore_start: bool = False) -> None:
        now = self.now()
        deadline = now + timedelta(minutes=self.c.max_runtime_minutes) if self.c.max_runtime_minutes else None
        if self.state.stopped:
            log.info("Мониторинг уже остановлен ранее: %s", self.state.stop_reason)
            return self._finish()
        if self.schedule.phase(now) == "after":
            log.info("MONITOR_STOP (%s) уже прошёл — проверки не выполняются.", self.c.monitor_stop.isoformat())
            return self._finish()
        if not self.state.started_notified:
            windows = "; ".join(
                f"{w.start:%d.%m %H:%M}–{w.end:%d.%m %H:%M} каждые {max(self.c.min_interval_seconds, w.interval_seconds)} с"
                for w in self.c.intensive_windows
            ) or "нет"
            start = now if ignore_start else max(now, self.c.monitor_start)
            if self._send(
                f"▶️ <b>Монитор запущен</b>\n{esc(self._route)}, дата {self._target}\n"
                f"Старт проверок: {start:%d.%m.%Y %H:%M}. Особые интервалы: {esc(windows)}; "
                f"далее каждые {max(self.c.min_interval_seconds, self.c.check_interval_seconds)} с "
                f"до обнаружения мест или {self.c.monitor_stop:%d.%m.%Y %H:%M}.\nЧасовой пояс: {self.c.tz.key}"
            ):
                self.state.started_notified = True
                self.store.save(self.state)

        def time_left() -> float | None:
            return None if deadline is None else (deadline - self.now()).total_seconds()

        if not ignore_start and self.schedule.phase(now) == "before":
            log.info("Ожидание начала мониторинга: %s", self.c.monitor_start.isoformat())
            self.update_status("waiting", self.c.monitor_start)
            target = self.c.monitor_start if deadline is None else min(self.c.monitor_start, deadline)
            self._sleep_until(target)

        while self.schedule.phase(self.now()) != "after":
            left = time_left()
            if left is not None and left <= 0:
                log.info("Достигнут MAX_RUNTIME_MINUTES — выхожу; следующий запуск продолжит с сохранённым состоянием.")
                return
            self._touch_health("active")
            self.heartbeat_if_due(self.now())
            delay = self.check_once()
            self.update_status("active", self.now() + timedelta(seconds=delay))
            if self.state.stopped:
                log.info("Остановка: %s", self.state.stop_reason)
                return self._finish()
            left = time_left()
            self.sleep(delay if left is None else max(0.0, min(delay, left)))

        log.info("Время мониторинга истекло (%s). Проверки остановлены.", self.c.monitor_stop.isoformat())
        self._send(f"⏹ Мониторинг завершён: наступило {self.c.monitor_stop:%d.%m.%Y %H:%M}. Проверки остановлены.")
        self.state.stopped, self.state.stop_reason = True, "истёк MONITOR_STOP"
        self.store.save(self.state)
        self.update_status("active")
        self._finish()
