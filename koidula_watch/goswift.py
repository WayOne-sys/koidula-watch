"""Чтение сетки свободных слотов из публичного мастера бронирования GoSwift.

Официального API у GoSwift нет. Этот модуль повторяет то, что делает браузер
посетителя на шагах 1–4 мастера бронирования, без ввода каких-либо данных
водителя или автомобиля (они запрашиваются только на шагах 5–6):

  1. POST preReserveSelectWaitingArea.action  — категория ТС (B)
  2. POST preReserveSelectQueueType.action    — пункт пропуска (Koidula = 2)
  3. POST preReserveSelectQueueType.action    — тип очереди «Reserve for certain time»
  4. GET  findOpenTimeslot.action?preferredDate=DD.MM.YYYY
         — HTML-фрагмент: 4 дня × 24 часовых слота

Шаги 1–3 выполняются один раз на сессию; далее каждая проверка — один
лёгкий GET (около 1,3 КБ в gzip). Монитор ничего не бронирует.

Разметка слота (наблюдалась 29.10.2026):
  <div class="slotContainer slotLocked" id="timeslot" data-time="20.12.2026 00:00"> Not available </div>
  <div class="slotContainer slotLocked" ...> Full </div>
Бронируемый слот: класс ``slotFree`` (так его выбирает скрипт страницы:
``$('.slotContainer.slotFree, .slotContainer.slot_selected').on('click', ...)``).
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)


class GoSwiftError(Exception):
    """Любая ошибка получения или разбора данных."""


class SessionInitError(GoSwiftError):
    pass


class GridMissingError(GoSwiftError):
    """В ответе нет сетки слотов (сессия истекла или изменилась вёрстка)."""


class TargetDateMissingError(GoSwiftError):
    """Сайт вернул сетку, но в ней нет запрошенной даты."""


class RateLimitedError(GoSwiftError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class SlotState(enum.Enum):
    FREE = "free"            # можно забронировать
    FULL = "full"            # слот открыт, но занят
    NOT_OPEN = "not_open"    # «Not available» — бронирование не открыто
    LOCKED = "locked"        # заблокирован, текст не распознан
    UNKNOWN = "unknown"      # нет ни slotFree, ни slotLocked — вёрстка изменилась


class DayStatus(enum.Enum):
    AVAILABLE = "available"        # есть хотя бы один slotFree
    OPENED_FULL = "opened_full"    # открыт, свободных нет
    NOT_OPEN = "not_open"          # все слоты «Not available»
    UNCLEAR = "unclear"            # есть нераспознанные состояния


@dataclass(frozen=True)
class Slot:
    start: datetime  # наивное местное время Europe/Tallinn, как на сайте
    state: SlotState
    text: str
    classes: tuple[str, ...]


@dataclass
class DayResult:
    target_date: date
    status: DayStatus
    slots: list[Slot] = field(default_factory=list)

    def hours(self, state: SlotState) -> list[str]:
        return [s.start.strftime("%H:%M") for s in self.slots if s.state == state]

    @property
    def free_hours(self) -> list[str]:
        return self.hours(SlotState.FREE)

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for slot in self.slots:
            result[slot.state.value] = result.get(slot.state.value, 0) + 1
        return result


_FULL_TEXTS = {"full", "täis", "занято", "заполнено", "мест нет"}
_NOT_OPEN_TEXTS = {"not available", "pole saadaval", "недоступно", "нет в наличии"}


def _classify(classes: set[str], text: str) -> SlotState:
    normalized = " ".join(text.lower().split())
    if "slotFree" in classes:
        return SlotState.FREE
    if "slotLocked" in classes:
        if normalized in _FULL_TEXTS:
            return SlotState.FULL
        if normalized in _NOT_OPEN_TEXTS:
            return SlotState.NOT_OPEN
        return SlotState.LOCKED
    return SlotState.UNKNOWN


def parse_slots(html: str) -> list[Slot]:
    """Разбирает HTML-фрагмент findOpenTimeslot.action."""
    soup = BeautifulSoup(html, "html.parser")
    containers = soup.select(".slotContainer[data-time]")
    if not containers:
        raise GridMissingError("В ответе нет элементов .slotContainer[data-time]")
    slots: dict[datetime, Slot] = {}
    for element in containers:
        raw_time = element.get("data-time", "").strip()
        try:
            start = datetime.strptime(raw_time, "%d.%m.%Y %H:%M")
        except ValueError as exc:
            raise GridMissingError(f"Неожиданный формат data-time: {raw_time!r}") from exc
        classes = tuple(element.get("class", []))
        text = element.get_text(" ", strip=True)
        # Дубликаты (если сайт добавит мобильную вёрстку) — оставляем первый.
        slots.setdefault(start, Slot(start, _classify(set(classes), text), text, classes))
    return sorted(slots.values(), key=lambda s: s.start)


def evaluate_day(slots: list[Slot], target: date) -> DayResult:
    """Оценивает только слоты целевой даты. Другие даты не учитываются."""
    day_slots = [s for s in slots if s.start.date() == target]
    if not day_slots:
        returned = sorted({s.start.date().isoformat() for s in slots})
        raise TargetDateMissingError(
            f"В сетке нет даты {target.isoformat()}; сайт вернул: {', '.join(returned) or 'ничего'}"
        )
    states = {s.state for s in day_slots}
    if SlotState.FREE in states:
        status = DayStatus.AVAILABLE
    elif states & {SlotState.UNKNOWN, SlotState.LOCKED}:
        status = DayStatus.UNCLEAR
    elif SlotState.FULL in states:
        status = DayStatus.OPENED_FULL
    else:
        status = DayStatus.NOT_OPEN
    return DayResult(target, status, day_slots)


class GoSwiftClient:
    def __init__(
        self,
        base_url: str,
        vehicle_category: str,
        border_point_id: str,
        queue_type: str,
        timeout: float = 20.0,
        user_agent: str = "KoidulaQueueWatch/1.0",
        session_factory=requests.Session,
    ):
        self.base_url = base_url.rstrip("/")
        self.vehicle_category = vehicle_category
        self.border_point_id = border_point_id
        self.queue_type = queue_type
        self.timeout = timeout
        self.user_agent = user_agent
        self._session_factory = session_factory
        self._session: requests.Session | None = None
        self.session_inits = 0

    def _url(self, path: str) -> str:
        return f"{self.base_url}/yphis/{path}"

    def _check(self, response: requests.Response, step: str) -> requests.Response:
        if response.status_code in (429, 503):
            retry_after = response.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else None
            except ValueError:
                delay = None
            raise RateLimitedError(f"{step}: HTTP {response.status_code}", delay)
        if response.status_code >= 400:
            raise GoSwiftError(f"{step}: HTTP {response.status_code}")
        return response

    def _new_session(self) -> requests.Session:
        session = self._session_factory()
        session.headers.update(
            {
                "User-Agent": self.user_agent,
                "Accept-Language": "en",
                "Accept": "text/html,application/xhtml+xml",
            }
        )
        return session

    def init_session(self) -> None:
        """Шаги 1–3 мастера. Никаких персональных данных не отправляется."""
        session = self._new_session()
        try:
            self._check(
                session.get(self._url("preReserveSelectVehicle.action"),
                            params={"request_locale": "en"}, timeout=self.timeout),
                "шаг 0 (открытие мастера)",
            )
            self._check(
                session.post(self._url("preReserveSelectWaitingArea.action"),
                             data={"placeInQueue.vehicleInQueue.vehicleCategory.name": self.vehicle_category},
                             timeout=self.timeout),
                "шаг 1 (категория)",
            )
            self._check(
                session.post(self._url("preReserveSelectQueueType.action"),
                             data={"placeInQueue.borderCrossingPoint.id": self.border_point_id},
                             timeout=self.timeout),
                "шаг 2 (пункт пропуска)",
            )
            step3 = self._check(
                session.post(self._url("preReserveSelectQueueType.action"),
                             data={"queueType": self.queue_type, "action:preReserveSelectTimeslot": "Next"},
                             timeout=self.timeout),
                "шаг 3 (тип очереди)",
            )
        except requests.RequestException as exc:
            raise SessionInitError(f"Сетевая ошибка при открытии сессии: {exc}") from exc
        if "findOpenTimeslot" not in step3.text:
            raise SessionInitError(
                "Шаг 3 не привёл к странице выбора времени — возможно, изменился мастер бронирования"
            )
        self._session = session
        self.session_inits += 1
        log.info("Сессия GoSwift открыта (всего открытий: %d)", self.session_inits)

    def fetch_slots_html(self, target: date) -> str:
        if self._session is None:
            self.init_session()
        assert self._session is not None
        try:
            response = self._session.get(
                self._url("findOpenTimeslot.action"),
                params={"preferredDate": target.strftime("%d.%m.%Y"), "_": int(time.time() * 1000)},
                headers={"X-Requested-With": "XMLHttpRequest"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            self._session = None
            raise GoSwiftError(f"Сетевая ошибка: {exc}") from exc
        self._check(response, "проверка слотов")
        return response.text

    def check_day(self, target: date) -> DayResult:
        """Один цикл проверки. При истёкшей сессии — одно переоткрытие и повтор."""
        html = self.fetch_slots_html(target)
        try:
            slots = parse_slots(html)
        except GridMissingError:
            log.info("Сетка не получена — переоткрываю сессию и повторяю один раз")
            self._session = None
            html = self.fetch_slots_html(target)
            slots = parse_slots(html)
        return evaluate_day(slots, target)
