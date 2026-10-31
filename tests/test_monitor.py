from datetime import date, datetime, timedelta

import pytest

from koidula_watch.config import load_config
from koidula_watch.fixtures import scenario_html
from koidula_watch.goswift import GoSwiftError, RateLimitedError, evaluate_day, parse_slots
from koidula_watch.monitor import Monitor, Schedule
from koidula_watch.notifier import NotifyError
from koidula_watch.state import CheckJournal, StateStore

TARGET = date(2026, 12, 20)


@pytest.fixture
def config(tmp_path):
    return load_config({"DATA_DIR": str(tmp_path), "ERROR_ALERT_THRESHOLD": "3",
                        "TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_CHAT_ID": "1",
                        "STOP_ON_AVAILABLE": "false"})


class Clock:
    def __init__(self, config, start="2026-11-01T00:00:05"):
        self.t = datetime.fromisoformat(start).replace(tzinfo=config.tz)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


class Notes:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, text, attempts=3):
        if self.fail:
            raise NotifyError("down")
        self.sent.append(text)


class Seq:
    """Возвращает заданные сценарии/исключения по очереди."""

    def __init__(self, *items):
        self.items = list(items)

    def check_day(self, target):
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return evaluate_day(parse_slots(scenario_html(item, target)), target)


def make(config, checker, notes, clock):
    return Monitor(config, checker, notes, StateStore(config.data_dir / "s.json"),
                   CheckJournal(config.data_dir / "j.jsonl"), now_fn=clock, sleep_fn=lambda s: None)


def test_not_open_sends_nothing(config):
    notes = Notes()
    make(config, Seq("not_open"), notes, Clock(config)).check_once()
    assert notes.sent == []


def test_available_notifies_once_then_deduplicates(config):
    notes, clock = Notes(), Clock(config)
    m = make(config, Seq("free", "free", "free"), notes, clock)
    m.check_once(); clock.advance(60); m.check_once(); clock.advance(60); m.check_once()
    assert len(notes.sent) == 1
    assert "20.12.2026" in notes.sent[0] and "10:00, 14:00" in notes.sent[0]


def test_dedup_survives_restart(config):
    notes, clock = Notes(), Clock(config)
    make(config, Seq("free"), notes, clock).check_once()
    clock.advance(60)
    make(config, Seq("free"), notes, clock).check_once()  # новый процесс, то же состояние
    assert len(notes.sent) == 1


def test_slot_reappearing_after_cooldown_notifies_again(config):
    notes, clock = Notes(), Clock(config)
    m = make(config, Seq("free", "full", "free", "full", "free"), notes, clock)
    m.check_once()                      # уведомление
    clock.advance(60); m.check_once()   # исчезли (+ одно «открыт, но занят»)
    clock.advance(60); m.check_once()   # вернулись через 2 мин — в пределах 15 мин, подавлено
    clock.advance(20 * 60); m.check_once()
    clock.advance(60); m.check_once()   # вернулись после паузы > 15 мин — снова уведомление
    available = [t for t in notes.sent if "Есть свободные слоты" in t]
    assert len(available) == 2


def test_opened_full_is_reported_once_and_not_as_availability(config):
    notes, clock = Notes(), Clock(config)
    m = make(config, Seq("full", "full"), notes, clock)
    m.check_once(); clock.advance(60); m.check_once()
    assert len(notes.sent) == 1
    assert "свободных мест нет" in notes.sent[0]
    assert "Есть свободные слоты" not in notes.sent[0]


def test_other_day_free_is_not_reported_as_available(config):
    notes = Notes()
    make(config, Seq("other_day_free"), notes, Clock(config)).check_once()
    assert not any("Есть свободные слоты" in t for t in notes.sent)


def test_failed_delivery_is_retried_next_check(config):
    clock = Clock(config)
    failing = Notes(fail=True)
    make(config, Seq("free"), failing, clock).check_once()
    ok = Notes()
    clock.advance(60)
    make(config, Seq("free"), ok, clock).check_once()
    assert len(ok.sent) == 1


def test_error_alert_after_threshold_then_recovery(config):
    notes, clock = Notes(), Clock(config)
    err = GoSwiftError("timeout")
    m = make(config, Seq(err, err, err, err, "not_open"), notes, clock)
    delays = []
    for _ in range(5):
        delays.append(m.check_once()); clock.advance(60)
    errors = [t for t in notes.sent if "Ошибка мониторинга" in t]
    assert len(errors) == 1                      # 3-я ошибка → алерт, 4-я — в пределах 30 мин
    assert "Ошибка мониторинга" in notes.sent[0] and "восстановлен" in notes.sent[-1]
    assert delays[1] > delays[0]                 # экспоненциальная пауза


def test_rate_limit_respects_retry_after(config):
    delay = make(config, Seq(RateLimitedError("429", 300)), Notes(), Clock(config)).check_once()
    assert delay >= 300


def test_journal_written(config):
    make(config, Seq("free"), Notes(), Clock(config)).check_once()
    lines = (config.data_dir / "j.jsonl").read_text().splitlines()
    assert len(lines) == 1 and '"status": "available"' in lines[0]


# ── расписание ────────────────────────────────────────────────────────────
def at(config, s):
    return datetime.fromisoformat(s).replace(tzinfo=config.tz)


def test_schedule_phases_and_intervals(config):
    s = Schedule(config)
    assert s.phase(at(config, "2026-10-31T22:59")) == "before"
    assert s.phase(at(config, "2026-10-31T23:00")) == "active"
    assert s.base_interval(at(config, "2026-10-31T23:10")) == 900   # 23:00–00:00 каждые 15 мин
    assert s.base_interval(at(config, "2026-11-01T00:00")) == 60    # 00:00–00:30 каждую минуту
    assert s.base_interval(at(config, "2026-11-01T00:29:59")) == 60
    assert s.base_interval(at(config, "2026-11-01T00:30")) == 300   # далее каждые 5 мин
    assert s.base_interval(at(config, "2026-11-20T12:00")) == 300
    assert s.phase(at(config, "2026-12-20T19:00")) == "after"


def test_schedule_wakes_up_exactly_at_midnight(config):
    s = Schedule(config)
    # 23:45 + 15 мин с джиттером могло бы перескочить 00:00 — должно проснуться ровно в 00:00
    assert s.next_delay(at(config, "2026-10-31T23:45:00")) == pytest.approx(900.0)
    assert s.next_delay(at(config, "2026-10-31T23:50:00")) == pytest.approx(600.0)


def test_interval_never_below_hard_floor(tmp_path):
    cfg = load_config({"DATA_DIR": str(tmp_path), "CHECK_INTERVAL_SECONDS": "1", "MIN_INTERVAL_SECONDS": "1"})
    assert Schedule(cfg).base_interval(at(cfg, "2026-11-05T12:00")) == 20


def test_tallinn_dst_end_handled(config):
    # 25.10.2026 переход на зимнее время уже прошёл: 31.10 23:00 = 21:00 UTC (UTC+2)
    assert config.monitor_start.utcoffset() == timedelta(hours=2)


# ── остановка и продолжение между запусками (GitHub Actions) ────────────────
def cfg_gh(tmp_path, **extra):
    env = {"DATA_DIR": str(tmp_path), "TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_CHAT_ID": "1",
           "EXIT_WHEN_DONE": "true", "MAX_RUNTIME_MINUTES": "60", **extra}
    return load_config(env)


class Endless:
    def __init__(self, scenario_at):
        self.scenario_at = scenario_at

    def check_day(self, target):
        return evaluate_day(parse_slots(scenario_html(self.scenario_at(), target)), target)


def run(cfg, clock, checker, notes):
    m = make(cfg, checker, notes, clock)
    m.sleep = clock.advance
    m.run_forever()
    return m


def test_stop_on_available_stops_and_next_run_does_nothing(tmp_path):
    cfg = cfg_gh(tmp_path)
    clock, notes = Clock(cfg, "2026-11-01T00:00:00"), Notes()
    m = run(cfg, clock, Endless(lambda: "free"), notes)
    assert m.state.stopped and m.state.checks_total == 1
    assert sum("Есть свободные слоты" in t for t in notes.sent) == 1
    again = Notes()
    m2 = run(cfg, clock, Endless(lambda: "free"), again)
    assert again.sent == [] and m2.state.checks_total == 1


def test_max_runtime_then_resume_without_duplicate_start_message(tmp_path):
    cfg = cfg_gh(tmp_path)
    clock, notes = Clock(cfg, "2026-11-01T01:00:00"), Notes()
    m = run(cfg, clock, Endless(lambda: "not_open"), notes)
    assert 11 <= m.state.checks_total <= 13              # ~60 мин / 5 мин
    assert sum("Монитор запущен" in t for t in notes.sent) == 1
    m2 = run(cfg, clock, Endless(lambda: "not_open"), notes)
    assert sum("Монитор запущен" in t for t in notes.sent) == 1
    assert m2.state.checks_total > m.state.checks_total


def test_full_night_timeline(tmp_path):
    """22:45 старт → ждёт 23:00 → 4 проверки по 15 мин → 30 по минуте → 5 мин; открытие в 00:07."""
    cfg = cfg_gh(tmp_path, MAX_RUNTIME_MINUTES="0")
    clock, notes = Clock(cfg, "2026-10-31T22:45:00"), Notes()
    opened = at(cfg, "2026-11-01T00:07:00")
    m = run(cfg, clock, Endless(lambda: "free" if clock() >= opened else "not_open"), notes)
    import json
    recs = [json.loads(l) for l in (cfg.data_dir / "j.jsonl").read_text().splitlines()]
    times = [datetime.fromisoformat(r["ts"]) for r in recs]
    assert times[0] == at(cfg, "2026-10-31T23:00:00")
    assert sum(t < at(cfg, "2026-11-01T00:00") for t in times) == 4
    assert at(cfg, "2026-11-01T00:00") in times
    found = next(t for t, r in zip(times, recs) if r.get("status") == "available")
    assert (found - opened).total_seconds() <= 65
    assert m.state.stopped


class EditNotes(Notes):
    def __init__(self):
        super().__init__()
        self.edits, self.pins = [], []

    def send(self, text, attempts=3):
        self.sent.append(text)
        return len(self.sent)

    def edit(self, mid, text):
        self.edits.append((mid, text))

    def pin(self, mid):
        self.pins.append(mid)


def test_live_status_message_created_once_then_edited(tmp_path):
    cfg = cfg_gh(tmp_path, MAX_RUNTIME_MINUTES="5")
    clock, notes = Clock(cfg, "2026-11-01T00:00:00"), EditNotes()
    m = run(cfg, clock, Endless(lambda: "not_open"), notes)
    status_msgs = [t for t in notes.sent if "Статус монитора" in t]
    assert len(status_msgs) == 1 and len(notes.pins) == 1
    assert len(notes.edits) >= 3                       # ~5 проверок за 5 мин по 60 с
    last = notes.edits[-1][1]
    assert "🟢" in last and "ещё не открыто" in last and "Последняя проверка" in last
    # новый сеанс продолжает редактировать то же сообщение
    run(cfg, clock, Endless(lambda: "not_open"), notes)
    assert len([t for t in notes.sent if "Статус монитора" in t]) == 1


def test_status_shows_errors(tmp_path):
    cfg = cfg_gh(tmp_path, MAX_RUNTIME_MINUTES="2")
    clock, notes = Clock(cfg, "2026-11-01T00:00:00"), EditNotes()

    class Fail:
        def check_day(self, t):
            raise GoSwiftError("boom")

    run(cfg, clock, Fail(), notes)
    assert "🟠" in notes.edits[-1][1]


def test_multi_notifier_status_only_primary(tmp_path):
    from koidula_watch.notifier import MultiNotifier
    cfg = cfg_gh(tmp_path, MAX_RUNTIME_MINUTES="2")
    primary, wife = EditNotes(), EditNotes()
    multi = MultiNotifier(primary, [wife])
    clock = Clock(cfg, "2026-11-01T00:00:00")
    run(cfg, clock, Endless(lambda: "free"), multi)
    assert any("Есть свободные слоты" in t for t in wife.sent)
    assert any("Есть свободные слоты" in t for t in primary.sent)
    assert not any("Статус монитора" in t for t in wife.sent)
    assert any("Статус монитора" in t for t in primary.sent)
