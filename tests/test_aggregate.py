"""Tests for aggregate_reports — the core business logic."""
import pytest

import bot


def row(date, sp, m, v):
    """Shorthand row builder mimicking psycopg2 RealDictCursor output."""
    return {"date": date, "specialist": sp, "metric": m, "value": v}


# ============================================================
# Empty / single-row cases
# ============================================================

class TestAggregateBasics:
    def test_empty_input_returns_empty_dict(self):
        assert bot.aggregate_reports([]) == {}

    def test_single_numeric_row(self):
        rows = [row("2026-05-01", "Stas", "контакты", "5")]
        r = bot.aggregate_reports(rows)
        assert r == {
            "Stas": {
                "metrics": {"контакты": 5},
                "comments": [],
                "days_submitted": 1,
                "averages": {"контакты": 5},
                "series": {"контакты": [{"date": "2026-05-01", "value": 5}]},
            }
        }

    def test_returns_int_when_value_is_whole(self):
        rows = [row("2026-05-01", "S", "x", "5")]
        r = bot.aggregate_reports(rows)
        assert r["S"]["metrics"]["x"] == 5
        assert isinstance(r["S"]["metrics"]["x"], int)

    def test_returns_float_when_value_has_fraction(self):
        rows = [row("2026-05-01", "S", "x", "5.5")]
        r = bot.aggregate_reports(rows)
        assert r["S"]["metrics"]["x"] == 5.5
        assert isinstance(r["S"]["metrics"]["x"], float)


# ============================================================
# Text fields routed to comments
# ============================================================

class TestTextFields:
    def test_text_key_value_goes_to_comments_not_metrics(self):
        rows = [row("2026-05-01", "S", "коммент", "был на встрече")]
        r = bot.aggregate_reports(rows, text_keys={"коммент"})
        assert r["S"]["metrics"] == {}
        assert r["S"]["comments"] == [
            {"date": "2026-05-01", "metric": "коммент", "value": "был на встрече"}
        ]

    def test_dash_excluded_from_comments(self):
        """Sentinel '-' (no comment today) shouldn't appear in dashboard."""
        rows = [row("2026-05-01", "S", "коммент", "-")]
        r = bot.aggregate_reports(rows, text_keys={"коммент"})
        assert r["S"]["comments"] == []

    def test_empty_string_excluded_from_comments(self):
        rows = [row("2026-05-01", "S", "коммент", "")]
        r = bot.aggregate_reports(rows, text_keys={"коммент"})
        assert r["S"]["comments"] == []

    def test_whitespace_only_excluded(self):
        rows = [row("2026-05-01", "S", "коммент", "   ")]
        r = bot.aggregate_reports(rows, text_keys={"коммент"})
        assert r["S"]["comments"] == []

    def test_non_numeric_value_falls_back_to_comments(self):
        """Numeric metric with text value (e.g., user typed 'много')
        should not crash — value goes to comments."""
        rows = [row("2026-05-01", "S", "контакты", "много")]
        r = bot.aggregate_reports(rows)  # no text_keys — heuristic
        assert r["S"]["metrics"] == {}
        assert len(r["S"]["comments"]) == 1
        assert r["S"]["comments"][0]["value"] == "много"


# ============================================================
# Multi-day aggregation (the main feature)
# ============================================================

class TestMultiDay:
    def test_sum_across_days(self):
        rows = [
            row("2026-05-01", "S", "контакты", "5"),
            row("2026-05-02", "S", "контакты", "3"),
            row("2026-05-03", "S", "контакты", "7"),
        ]
        r = bot.aggregate_reports(rows)
        assert r["S"]["metrics"]["контакты"] == 15

    def test_days_submitted_counts_unique_dates(self):
        rows = [
            row("2026-05-01", "S", "контакты", "5"),
            row("2026-05-01", "S", "кп", "2"),       # same day, different metric
            row("2026-05-02", "S", "контакты", "3"),
        ]
        r = bot.aggregate_reports(rows)
        assert r["S"]["days_submitted"] == 2

    def test_averages_use_days_submitted(self):
        rows = [
            row("2026-05-01", "S", "x", "10"),
            row("2026-05-02", "S", "x", "20"),
        ]
        r = bot.aggregate_reports(rows)
        assert r["S"]["averages"]["x"] == 15  # (10+20)/2

    def test_series_sorted_ascending_by_date(self):
        rows = [
            row("2026-05-03", "S", "x", "7"),
            row("2026-05-01", "S", "x", "5"),
            row("2026-05-02", "S", "x", "3"),
        ]
        r = bot.aggregate_reports(rows)
        dates = [p["date"] for p in r["S"]["series"]["x"]]
        assert dates == ["2026-05-01", "2026-05-02", "2026-05-03"]

    def test_same_metric_same_day_summed_in_series(self):
        """If a specialist accidentally submitted twice on same day,
        values for that day are summed."""
        rows = [
            row("2026-05-01", "S", "x", "5"),
            row("2026-05-01", "S", "x", "3"),
        ]
        r = bot.aggregate_reports(rows)
        assert r["S"]["series"]["x"] == [{"date": "2026-05-01", "value": 8}]


# ============================================================
# Multiple specialists / metrics
# ============================================================

class TestMultiSpecialist:
    def test_specialists_grouped_separately(self):
        rows = [
            row("2026-05-01", "Эльдана", "заявки", "10"),
            row("2026-05-01", "Станислав", "контакты", "5"),
        ]
        r = bot.aggregate_reports(rows)
        assert set(r.keys()) == {"Эльдана", "Станислав"}
        assert r["Эльдана"]["metrics"] == {"заявки": 10}
        assert r["Станислав"]["metrics"] == {"контакты": 5}

    def test_mixed_numeric_and_text_for_one_specialist(self):
        rows = [
            row("2026-05-01", "S", "контакты", "5"),
            row("2026-05-01", "S", "коммент", "встреча"),
        ]
        r = bot.aggregate_reports(rows, text_keys={"коммент"})
        assert r["S"]["metrics"] == {"контакты": 5}
        assert len(r["S"]["comments"]) == 1


# ============================================================
# Real-world value formats
# ============================================================

class TestValueFormats:
    def test_decimal_with_russian_comma(self):
        rows = [row("2026-05-01", "S", "доход", "15 000,50")]
        r = bot.aggregate_reports(rows)
        assert r["S"]["metrics"]["доход"] == 15000.50

    def test_summing_with_mixed_separators(self):
        rows = [
            row("2026-05-01", "S", "доход", "1 000"),
            row("2026-05-02", "S", "доход", "1500,5"),
        ]
        r = bot.aggregate_reports(rows)
        assert r["S"]["metrics"]["доход"] == 2500.5
