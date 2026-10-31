from datetime import date
from pathlib import Path

import pytest

from koidula_watch.fixtures import scenario_html
from koidula_watch.goswift import (
    DayStatus, GridMissingError, SlotState, TargetDateMissingError, evaluate_day, parse_slots,
)

TARGET = date(2026, 12, 20)
REAL = Path(__file__).parent / "fixtures" / "real_20_12_2026_excerpt.html"


def test_real_markup_excerpt_is_not_open():
    """Фрагмент реального ответа сайта от 29.10.2026 (сокращён до 3 слотов на день)."""
    slots = parse_slots(REAL.read_text(encoding="utf-8"))
    assert {s.start.date() for s in slots} == {date(2026, 12, d) for d in (20, 21, 22, 23)}
    result = evaluate_day(slots, TARGET)
    assert result.status == DayStatus.NOT_OPEN
    assert result.free_hours == []


def test_full_markup_from_real_site():
    html = '<div class="slotContainer slotLocked" id="timeslot" data-time="20.12.2026 12:00"> Full </div>'
    (slot,) = parse_slots(html)
    assert slot.state == SlotState.FULL


@pytest.mark.parametrize("scenario,status,free", [
    ("not_open", DayStatus.NOT_OPEN, []),
    ("full", DayStatus.OPENED_FULL, []),
    ("free", DayStatus.AVAILABLE, ["10:00", "14:00"]),
])
def test_scenarios(scenario, status, free):
    result = evaluate_day(parse_slots(scenario_html(scenario, TARGET)), TARGET)
    assert result.status == status
    assert result.free_hours == free
    assert len(result.slots) == 24


def test_free_slot_on_other_day_is_not_availability_for_target():
    result = evaluate_day(parse_slots(scenario_html("other_day_free", TARGET)), TARGET)
    assert result.status == DayStatus.OPENED_FULL
    assert result.free_hours == []


def test_target_date_missing_is_an_error_not_no_slots():
    html = scenario_html("free", date(2026, 12, 1))  # сайт вернул 01–04.12
    with pytest.raises(TargetDateMissingError):
        evaluate_day(parse_slots(html), TARGET)


def test_session_expired_page_raises():
    with pytest.raises(GridMissingError):
        parse_slots("<html><title>GoSwift | Select your vehicle category</title></html>")


def test_unknown_class_is_unclear_not_available():
    html = '<div class="slotContainer slotSomethingNew" data-time="20.12.2026 10:00"> ? </div>'
    result = evaluate_day(parse_slots(html), TARGET)
    assert result.status == DayStatus.UNCLEAR
