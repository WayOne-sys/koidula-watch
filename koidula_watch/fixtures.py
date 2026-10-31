"""Генератор HTML в формате ответа findOpenTimeslot.action.

Разметка повторяет реальный ответ, снятый 29.10.2026 для preferredDate=20.12.2026
(4 дня × 24 слота). Используется в тестах и в режиме `simulate`.
Состояние FREE сымитировано по классу `slotFree` из скрипта страницы —
реального свободного слота на момент разработки не было.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Callable

_TEXT = {"free": "Free", "full": "Full", "not_open": "Not available"}
_CLASS = {"free": "slotContainer slotFree", "full": "slotContainer slotLocked", "not_open": "slotContainer slotLocked"}


def make_grid_html(first_day: date, state_for: Callable[[date, int], str], days: int = 4) -> str:
    parts = [f'<div class="timeslots_desktop clearfix" data-preferreddate="{first_day:%d.%m.%Y}">']
    parts.append('<div class="first_container"><a class="datepicker_arrows nav-btn nav-btn-prev" href="#"></a>')
    parts += [f'<div class="first_slots"> {h:02d}:00 </div>' for h in range(24)]
    parts.append("</div>")
    for i in range(days):
        day = first_day + timedelta(days=i)
        extra = "" if i == 0 else " dayContainerNotFirst"
        parts.append(f'<div class="dayContainer{extra}"><div class="datepicker_wrapper">'
                     f'<input type="text" class="datepicker" placeholder="{day.day}. {day:%B}"></div>')
        for hour in range(24):
            state = state_for(day, hour)
            parts.append(
                f'<div class="{_CLASS[state]}" id="timeslot" data-time="{day:%d.%m.%Y} {hour:02d}:00">'
                f" {_TEXT[state]} </div>"
            )
        parts.append("</div>")
    parts.append("</div>")
    return "\n".join(parts)


def scenario_html(scenario: str, target: date) -> str:
    """Готовые сценарии: not_open, full, free, other_day_free."""
    if scenario == "not_open":
        return make_grid_html(target, lambda d, h: "not_open")
    if scenario == "full":
        return make_grid_html(target, lambda d, h: "full" if h % 2 == 0 else "not_open")
    if scenario == "free":
        return make_grid_html(
            target, lambda d, h: "free" if d == target and h in (10, 14) else ("full" if h % 2 == 0 else "not_open")
        )
    if scenario == "other_day_free":
        # Свободно 21.12, но не 20.12 — НЕ должно считаться доступностью.
        return make_grid_html(target, lambda d, h: "free" if d != target else "full")
    raise ValueError(f"Неизвестный сценарий: {scenario}")
