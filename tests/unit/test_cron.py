from datetime import datetime

import pytest

from lrclib_web.cron import Cron, CronError


def test_daily():
    c = Cron("0 4 * * *")
    assert c.matches(datetime(2026, 1, 5, 4, 0))
    assert not c.matches(datetime(2026, 1, 5, 4, 1))
    assert c.next_after(datetime(2026, 1, 5, 4, 0)) == datetime(2026, 1, 6, 4, 0)
    assert c.next_after(datetime(2026, 1, 5, 3, 59, 30)) == datetime(2026, 1, 5, 4, 0)


def test_weekly_sunday_0_and_7():
    # 2026-01-04 is a Sunday
    for expr in ("0 3 * * 0", "0 3 * * 7"):
        c = Cron(expr)
        assert c.next_after(datetime(2026, 1, 1)) == datetime(2026, 1, 4, 3, 0)


def test_steps_lists_ranges():
    c = Cron("*/15 9-10 * * 1,3")
    assert c.minutes == frozenset({0, 15, 30, 45})
    assert c.hours == frozenset({9, 10})
    assert c.dows == frozenset({1, 3})
    c2 = Cron("10-20/5 * * * *")
    assert c2.minutes == frozenset({10, 15, 20})


def test_dom_or_dow():
    c = Cron("0 0 13 * 5")  # 13th OR Friday
    assert c.matches(datetime(2026, 1, 13, 0, 0))  # Tuesday the 13th
    assert c.matches(datetime(2026, 1, 2, 0, 0))  # Friday the 2nd
    assert not c.matches(datetime(2026, 1, 3, 0, 0))


def test_month_rollover_and_feb29():
    c = Cron("0 0 1 3 *")
    assert c.next_after(datetime(2026, 3, 1, 0, 0)) == datetime(2027, 3, 1, 0, 0)
    assert Cron("0 0 29 2 *").next_after(datetime(2026, 1, 1)) == datetime(2028, 2, 29, 0, 0)


@pytest.mark.parametrize(
    "expr",
    ["", "* * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *", "a * * * *",
     "*/0 * * * *", "5-1 * * * *", "1,,2 * * * *"],
)
def test_invalid(expr):
    with pytest.raises(CronError):
        Cron(expr)
