from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

# 各司法辖区的官方期限以当地日期为准，这里给出对应的 IANA 时区。
JURISDICTION_TIMEZONES = {
    "CN": "Asia/Shanghai",
    "US": "America/New_York",
    "EP": "Europe/Berlin",
    "JP": "Asia/Tokyo",
}

WEEKEND = (5, 6)  # 周六、周日


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        last = date(year, 12, 31)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _us_holidays(year: int) -> set[date]:
    return {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _last_weekday(year, 5, 0),
        _observed(date(year, 6, 19)),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 10, 0, 2),
        _observed(date(year, 11, 11)),
        _nth_weekday(year, 11, 3, 4),
        _observed(date(year, 12, 25)),
    }


def _ep_holidays(year: int) -> set[date]:
    easter = _easter(year)
    return {
        date(year, 1, 1),
        date(year, 1, 6),
        easter - timedelta(days=2),
        easter + timedelta(days=1),
        date(year, 5, 1),
        easter + timedelta(days=39),
        easter + timedelta(days=50),
        easter + timedelta(days=60),
        date(year, 8, 15),
        date(year, 10, 3),
        date(year, 11, 1),
        date(year, 12, 25),
        date(year, 12, 26),
    }


def _jp_equinox_day(year: int, *, spring: bool) -> int:
    base = 20.8431 if spring else 23.2488
    return int(base + 0.242194 * (year - 1980)) - (year - 1980) // 4


def _jp_holidays(year: int) -> set[date]:
    holidays = {
        date(year, 1, 1),
        _nth_weekday(year, 1, 0, 2),
        date(year, 2, 11),
        date(year, 2, 23),
        date(year, 3, _jp_equinox_day(year, spring=True)),
        date(year, 4, 29),
        date(year, 5, 3),
        date(year, 5, 4),
        date(year, 5, 5),
        _nth_weekday(year, 7, 0, 3),
        date(year, 8, 11),
        _nth_weekday(year, 9, 0, 3),
        date(year, 9, _jp_equinox_day(year, spring=False)),
        _nth_weekday(year, 10, 0, 2),
        date(year, 11, 3),
        date(year, 11, 23),
    }
    # 振替休日：节日落在周日时，顺延到下一个非节日。
    for day in list(holidays):
        if day.weekday() == 6:
            substitute = day + timedelta(days=1)
            while substitute in holidays:
                substitute += timedelta(days=1)
            holidays.add(substitute)
    return holidays


# 中国法定节假日为代表性示例日历（覆盖 2025-2031 年），
# 生产环境可替换为国务院正式节假日安排，不影响其余逻辑。
_CN_FIXED = {(1, 1), (5, 1), (10, 1), (10, 2), (10, 3)}
_CN_EXTRA: dict[int, set[tuple[int, int]]] = {
    2025: {(1, 28), (1, 29), (1, 30), (1, 31), (2, 1), (2, 2), (2, 3), (2, 4), (4, 4), (4, 5), (4, 6), (5, 2), (5, 3), (5, 4), (5, 5), (5, 31), (6, 1), (6, 2), (10, 4), (10, 5), (10, 6), (10, 7), (10, 8)},
    2026: {(2, 16), (2, 17), (2, 18), (2, 19), (2, 20), (2, 21), (2, 22), (4, 4), (4, 5), (4, 6), (5, 2), (5, 3), (5, 4), (5, 5), (6, 19), (6, 20), (6, 21), (9, 25), (10, 4), (10, 5), (10, 6), (10, 7)},
    2027: {(2, 5), (2, 6), (2, 7), (2, 8), (2, 9), (2, 10), (2, 11), (4, 3), (4, 4), (4, 5), (5, 2), (5, 3), (5, 4), (6, 7), (6, 8), (6, 9), (9, 15), (10, 4), (10, 5), (10, 6), (10, 7)},
    2028: {(1, 26), (1, 27), (1, 28), (1, 29), (1, 30), (1, 31), (2, 1), (4, 4), (5, 2), (5, 3), (5, 28), (5, 29), (5, 30), (10, 3), (10, 4), (10, 5), (10, 6), (10, 7)},
    2029: {(2, 12), (2, 13), (2, 14), (2, 15), (2, 16), (2, 17), (2, 18), (4, 4), (5, 2), (5, 3), (6, 16), (6, 17), (6, 18), (9, 22), (10, 4), (10, 5), (10, 6), (10, 7)},
    2030: {(2, 2), (2, 3), (2, 4), (2, 5), (2, 6), (2, 7), (2, 8), (4, 4), (4, 5), (4, 6), (5, 2), (5, 3), (5, 4), (5, 5), (6, 5), (6, 6), (6, 7), (9, 12), (10, 4), (10, 5), (10, 6), (10, 7)},
    2031: {(1, 22), (1, 23), (1, 24), (1, 25), (1, 26), (1, 27), (1, 28), (4, 4), (4, 5), (4, 6), (5, 2), (5, 3), (5, 4), (6, 24), (6, 25), (6, 26), (10, 1), (10, 4), (10, 5), (10, 6), (10, 7)},
}


def _cn_holidays(year: int) -> set[date]:
    days = {date(year, month, day) for month, day in _CN_FIXED}
    days.update(date(year, month, day) for month, day in _CN_EXTRA.get(year, set()))
    return days


_HOLIDAY_BUILDERS = {
    "CN": _cn_holidays,
    "US": _us_holidays,
    "EP": _ep_holidays,
    "JP": _jp_holidays,
}


def supported_jurisdictions() -> list[str]:
    return sorted(JURISDICTION_TIMEZONES)


def jurisdiction_timezone(jurisdiction: str) -> str:
    return JURISDICTION_TIMEZONES[jurisdiction]


def holidays(jurisdiction: str, year: int) -> set[date]:
    builder = _HOLIDAY_BUILDERS.get(jurisdiction)
    if builder is None:
        return set()
    return builder(year)


def is_working_day(jurisdiction: str, day: date) -> bool:
    if day.weekday() in WEEKEND:
        return False
    return day not in holidays(jurisdiction, day.year)


def adjust_forward(jurisdiction: str, day: date) -> tuple[date, list[str]]:
    """截止日落在非工作日时顺延到下一工作日，返回调整后的日期和被跳过的日期。"""
    skipped: list[str] = []
    current = day
    while not is_working_day(jurisdiction, current):
        skipped.append(current.isoformat())
        current += timedelta(days=1)
    return current, skipped


def remind_date_for(jurisdiction: str, due: date, lead_days: int) -> tuple[date, bool]:
    """提醒日按提前量向前取，落在非工作日时继续提前，保证临期事项不会被节假日吞掉。"""
    target = due - timedelta(days=lead_days)
    shifted = False
    while not is_working_day(jurisdiction, target):
        target -= timedelta(days=1)
        shifted = True
    return target, shifted


def local_date(jurisdiction: str, instant: datetime) -> date:
    """把任意时刻换算成司法辖区当地日期，逾期判断一律以该日期为准。"""
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return instant.astimezone(ZoneInfo(jurisdiction_timezone(jurisdiction))).date()
