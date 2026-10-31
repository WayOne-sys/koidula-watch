"""Клиент GoSwift на подставном HTTP: проверяем последовательность шагов и параметры."""

from datetime import date

import pytest

from koidula_watch.fixtures import scenario_html
from koidula_watch.goswift import DayStatus, GoSwiftClient, RateLimitedError, SessionInitError


class Resp:
    def __init__(self, text="", status=200, headers=None):
        self.text, self.status_code, self.headers = text, status, headers or {}


STEP3 = '<script>$.ajax({url: "/yphis/findOpenTimeslot.action"})</script>'


class FakeSession:
    instances = []

    def __init__(self):
        self.headers, self.calls = {}, []
        FakeSession.instances.append(self)

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url.rsplit("/", 1)[1], params))
        if "findOpenTimeslot" in url:
            return FakeSession.queue.pop(0)
        return Resp("<html>step0</html>")

    def post(self, url, data=None, timeout=None):
        self.calls.append(("POST", url.rsplit("/", 1)[1], data))
        if data and "queueType" in data:
            return Resp(STEP3)
        return Resp("<html>ok</html>")


@pytest.fixture(autouse=True)
def reset():
    FakeSession.instances.clear()
    FakeSession.queue = []


def client():
    return GoSwiftClient("https://example.test", "B", "2", "1", session_factory=FakeSession)


def test_flow_sends_no_personal_data_and_correct_params():
    FakeSession.queue = [Resp(scenario_html("not_open", date(2026, 12, 20)))]
    result = client().check_day(date(2026, 12, 20))
    assert result.status == DayStatus.NOT_OPEN
    calls = FakeSession.instances[0].calls
    assert [c[1] for c in calls] == [
        "preReserveSelectVehicle.action", "preReserveSelectWaitingArea.action",
        "preReserveSelectQueueType.action", "preReserveSelectQueueType.action", "findOpenTimeslot.action",
    ]
    assert calls[1][2] == {"placeInQueue.vehicleInQueue.vehicleCategory.name": "B"}
    assert calls[2][2] == {"placeInQueue.borderCrossingPoint.id": "2"}
    assert calls[3][2]["queueType"] == "1"
    assert calls[4][2]["preferredDate"] == "20.12.2026"
    sent = str([c[2] for c in calls])
    for forbidden in ("plate", "regNr", "driver", "passport", "phone", "email"):
        assert forbidden not in sent


def test_session_reused_between_checks():
    FakeSession.queue = [Resp(scenario_html("not_open", date(2026, 12, 20))) for _ in range(3)]
    c = client()
    for _ in range(3):
        c.check_day(date(2026, 12, 20))
    assert c.session_inits == 1


def test_expired_session_reinitialised_once():
    FakeSession.queue = [Resp("<html>session expired</html>"), Resp(scenario_html("free", date(2026, 12, 20)))]
    c = client()
    # первая сессия получает «пустой» ответ, вторая — сетку
    result = c.check_day(date(2026, 12, 20))
    assert c.session_inits == 2
    assert result.status == DayStatus.AVAILABLE


def test_429_raises_rate_limited():
    FakeSession.queue = [Resp("", 429, {"Retry-After": "120"})]
    with pytest.raises(RateLimitedError) as info:
        client().check_day(date(2026, 12, 20))
    assert info.value.retry_after == 120


def test_wizard_change_detected():
    class Broken(FakeSession):
        def post(self, url, data=None, timeout=None):
            return Resp("<html>something else</html>")

    with pytest.raises(SessionInitError):
        GoSwiftClient("https://example.test", "B", "2", "1", session_factory=Broken).check_day(date(2026, 12, 20))
