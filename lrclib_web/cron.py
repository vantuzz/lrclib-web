"""Minimal 5-field cron parser (minute hour day-of-month month day-of-week).

Supports `*`, lists `a,b`, ranges `a-b`, steps `*/n` and `a-b/n`. Day-of-week 0-7 (0 and 7 = Sunday).
Like Vixie cron: when both day-of-month and day-of-week are restricted, either may match.
"""

from __future__ import annotations

from datetime import datetime, timedelta

_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]


class CronError(ValueError):
    pass


def _parse_field(text: str, lo: int, hi: int) -> frozenset[int]:
    out: set[int] = set()
    for part in text.split(","):
        if not part:
            raise CronError(f"empty item in {text!r}")
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not s.isdigit() or int(s) < 1:
                raise CronError(f"bad step {s!r}")
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            if not (x.isdigit() and y.isdigit()):
                raise CronError(f"bad range {part!r}")
            a, b = int(x), int(y)
        elif part.isdigit():
            a = b = int(part)
            if step != 1:
                b = hi
        else:
            raise CronError(f"bad value {part!r}")
        if a < lo or b > hi or a > b:
            raise CronError(f"{part!r} out of range {lo}-{hi}")
        out.update(range(a, b + 1, step))
    return frozenset(out)


class Cron:
    def __init__(self, expr: str):
        parts = expr.split()
        if len(parts) != 5:
            raise CronError("cron expression needs 5 fields")
        self.expr = expr
        sets = [_parse_field(p, lo, hi) for p, (lo, hi) in zip(parts, _RANGES, strict=True)]
        self.minutes, self.hours, self.doms, self.months, dows = sets
        self.dows = frozenset(d % 7 for d in dows)
        self.dom_star = parts[2].startswith("*")
        self.dow_star = parts[4].startswith("*")

    def _day_ok(self, t: datetime) -> bool:
        dom_ok = t.day in self.doms
        dow_ok = (t.isoweekday() % 7) in self.dows
        if self.dom_star and self.dow_star:
            return True
        if self.dom_star:
            return dow_ok
        if self.dow_star:
            return dom_ok
        return dom_ok or dow_ok

    def matches(self, t: datetime) -> bool:
        return (
            t.minute in self.minutes
            and t.hour in self.hours
            and t.month in self.months
            and self._day_ok(t)
        )

    def next_after(self, t: datetime) -> datetime | None:
        """First matching minute strictly after t (search horizon ~5 years)."""
        t = t.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = t + timedelta(days=366 * 5)
        while t < limit:
            if t.month not in self.months:
                y, m = (t.year + 1, 1) if t.month == 12 else (t.year, t.month + 1)
                t = t.replace(year=y, month=m, day=1, hour=0, minute=0)
                continue
            if not self._day_ok(t):
                t = (t + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if t.hour not in self.hours:
                t = (t + timedelta(hours=1)).replace(minute=0)
                continue
            if t.minute not in self.minutes:
                t += timedelta(minutes=1)
                continue
            return t
        return None
