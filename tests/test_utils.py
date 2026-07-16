import datetime as dt

from mr_short.utils import inr, is_trading_day, weekday_sessions


def test_inr_indian_grouping():
    assert inr(1234567) == "Rs 12,34,567"
    assert inr(1234) == "Rs 1,234"
    assert inr(999) == "Rs 999"
    assert inr(0) == "Rs 0"
    assert inr(-4500) == "Rs -4,500"
    assert inr(10000000) == "Rs 1,00,00,000"


def test_weekday_sessions():
    fri = dt.date(2026, 7, 10)
    mon = dt.date(2026, 7, 13)
    assert weekday_sessions(fri, mon) == 1          # weekend skipped
    assert weekday_sessions(mon, dt.date(2026, 7, 17)) == 4
    assert weekday_sessions(mon, mon) == 0


def test_is_trading_day():
    assert is_trading_day(dt.date(2026, 7, 16))      # Thursday
    assert not is_trading_day(dt.date(2026, 7, 18))  # Saturday
    assert not is_trading_day(dt.date(2026, 7, 19))  # Sunday
