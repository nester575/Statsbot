"""Pure-function unit tests: no DB, no Flask, no Telegram."""
from datetime import date, timedelta

import pytest

import bot      # bot re-exports all helpers — keep tests using bot.* for ergonomics
import config
import helpers


# ============================================================
# parse_number
# ============================================================

class TestParseNumber:
    @pytest.mark.parametrize("inp,expected", [
        ("0", 0.0),
        ("5", 5.0),
        ("-3", -3.0),
        ("100", 100.0),
        ("3.14", 3.14),
        ("3,14", 3.14),                # Russian decimal comma
        ("0.5", 0.5),
        ("1 000", 1000.0),             # space as thousand separator
        ("1 000", 1000.0),        # non-breaking space (NBSP) — typed by mobile keyboards
        ("1 000,50", 1000.50),
        ("15 000,50", 15000.50),
        ("  5  ", 5.0),                # outer whitespace
        ("1e3", 1000.0),               # scientific notation
        ("000", 0.0),
    ])
    def test_valid_numbers(self, inp, expected):
        assert bot.parse_number(inp) == expected

    @pytest.mark.parametrize("inp", [
        None, "", "   ", "abc", "—", "5 заявок", "N/A", "много", "-",
        "1.2.3", "5/10", "0xff", "пять",
    ])
    def test_invalid_returns_none(self, inp):
        assert bot.parse_number(inp) is None

    def test_zero_is_valid_number(self):
        # "0" is a real number — important for «доход за день — 0»
        assert bot.parse_number("0") == 0.0

    def test_returns_float_type(self):
        assert isinstance(bot.parse_number("5"), float)


# ============================================================
# parse_hhmm
# ============================================================

class TestParseHHMM:
    @pytest.mark.parametrize("inp,expected", [
        ("00:00", (0, 0)),
        ("09:15", (9, 15)),
        ("12:30", (12, 30)),
        ("23:59", (23, 59)),
        ("9:5", (9, 5)),         # без ведущих нулей
        (" 09:00 ", (9, 0)),     # outer whitespace
    ])
    def test_valid(self, inp, expected):
        assert bot.parse_hhmm(inp) == expected

    @pytest.mark.parametrize("inp", [
        None, "", "  ", "24:00", "12:60", "abc", "12-00",
        "12:5x", "12.30", "-1:00", "9:99", "25:30", "1230",
        "12::30", ":30", "12:",
    ])
    def test_invalid_returns_none(self, inp):
        assert bot.parse_hhmm(inp) is None


# ============================================================
# is_working_day
# ============================================================

class TestIsWorkingDay:
    """Reference week: 2026-05-04 (Mon) … 2026-05-10 (Sun)."""

    @pytest.mark.parametrize("d,expected", [
        (date(2026, 5, 4),  True),   # Monday
        (date(2026, 5, 5),  True),   # Tuesday
        (date(2026, 5, 6),  True),   # Wednesday
        (date(2026, 5, 7),  True),   # Thursday
        (date(2026, 5, 8),  True),   # Friday
        (date(2026, 5, 9),  False),  # Saturday
        (date(2026, 5, 10), False),  # Sunday
    ])
    def test_weekday_logic(self, d, expected):
        assert bot.is_working_day(d) is expected

    def test_holiday_excluded(self, monkeypatch):
        """A weekday listed in HOLIDAYS becomes non-working."""
        # is_working_day reads config.HOLIDAYS at call time
        monkeypatch.setattr(config, "HOLIDAYS", {"2026-05-04"})
        assert bot.is_working_day(date(2026, 5, 4)) is False
        # Other weekdays still working
        assert bot.is_working_day(date(2026, 5, 5)) is True


# ============================================================
# period_range
# ============================================================

class TestPeriodRange:
    """period_range uses datetime.now(BISHKEK).date() — we freeze it."""

    @pytest.fixture
    def freeze_today(self, monkeypatch):
        """Freeze today to 2026-05-10 (Sunday) Bishkek time."""
        from datetime import datetime
        frozen = datetime(2026, 5, 10, 12, 0, tzinfo=config.BISHKEK)
        class FakeDateTime:
            @classmethod
            def now(cls, tz=None): return frozen
        # period_range lives in helpers.py and uses helpers.datetime
        monkeypatch.setattr(helpers, "datetime", FakeDateTime)
        return frozen.date()

    def test_week_returns_7_days(self, freeze_today):
        start, end = bot.period_range("week")
        assert end == freeze_today
        assert (end - start).days == 6  # 7 days inclusive

    def test_month_starts_at_first(self, freeze_today):
        start, end = bot.period_range("month")
        assert start == date(2026, 5, 1)
        assert end == freeze_today

    def test_unknown_period_falls_back_to_week(self, freeze_today):
        # period_range itself accepts any string; only "month" is special.
        # The endpoint validates the period name; here we just ensure no crash.
        start, end = bot.period_range("xyz")
        assert (end - start).days == 6
