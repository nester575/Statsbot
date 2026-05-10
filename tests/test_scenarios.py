"""End-to-end scenarios from real life — multi-step flows that combine
helpers, aggregate logic, and API responses.

Each test simulates a realistic case the boss/admin actually does.
"""
import pytest

import bot
import config
import db


def row(date, sp, m, v):
    return {"date": date, "specialist": sp, "metric": m, "value": v}


# ============================================================
# Scenario 1: Plan progress visualization
# ============================================================

class TestPlanProgressScenarios:
    """План задан в админке, специалист сабмитит данные → дашборд показывает чип прогресса."""

    def test_plan_under_target_red(self):
        """45/100 = 45% → red zone (<50%)."""
        rows = [
            row("2026-05-04", "S", "контакты", "10"),
            row("2026-05-05", "S", "контакты", "15"),
            row("2026-05-06", "S", "контакты", "20"),
        ]
        agg = bot.aggregate_reports(rows)
        assert agg["S"]["metrics"]["контакты"] == 45
        # Frontend would compute: 45/100*100 = 45%, class "bad"

    def test_plan_on_track_yellow(self):
        """55/100 = 55% → yellow (50-79%)."""
        rows = [
            row("2026-05-04", "S", "контакты", "30"),
            row("2026-05-05", "S", "контакты", "25"),
        ]
        agg = bot.aggregate_reports(rows)
        assert agg["S"]["metrics"]["контакты"] == 55

    def test_plan_almost_done_green(self):
        """85/100 = 85% → green (80-99%)."""
        rows = [
            row("2026-05-04", "S", "контакты", "85"),
        ]
        agg = bot.aggregate_reports(rows)
        assert agg["S"]["metrics"]["контакты"] == 85

    def test_plan_overshoot_gold(self):
        """130/100 = 130% → gold/over."""
        rows = [
            row("2026-05-04", "S", "контакты", "70"),
            row("2026-05-05", "S", "контакты", "60"),
        ]
        agg = bot.aggregate_reports(rows)
        assert agg["S"]["metrics"]["контакты"] == 130

    def test_plan_exactly_met(self):
        """50/50 = 100% → over (gold)."""
        rows = [row("2026-05-04", "S", "x", "50")]
        agg = bot.aggregate_reports(rows)
        assert agg["S"]["metrics"]["x"] == 50


# ============================================================
# Scenario 2: Renaming a metric preserves history
# ============================================================

class TestRenameMetricPreservesHistory:
    """Bot.py docs: metric_key never changes, only display_name. So old reports
    still aggregate correctly after rename."""

    def test_old_data_still_aggregated_under_same_key(self):
        # Reports stored with internal key "заявки" — historical data
        rows = [
            row("2026-04-28", "Эльдана", "заявки", "5"),
            row("2026-04-29", "Эльдана", "заявки", "10"),
            row("2026-05-01", "Эльдана", "заявки", "8"),
        ]
        agg = bot.aggregate_reports(rows)
        # Even after admin renames display_name to "лиды", the metric_key
        # in reports is still "заявки", so sum is preserved
        assert agg["Эльдана"]["metrics"]["заявки"] == 23

    def test_display_names_translation_in_dashboard_response(self, client, monkeypatch, auth_headers):
        """display_names map should translate metric_key → user-visible name."""
        monkeypatch.setattr(db, "get_today_reports", lambda: [
            {"date": "2026-05-10", "time": "10:00", "specialist": "Эльдана",
             "metric": "заявки", "value": "5"}
        ])
        # Admin renamed "заявки" → "лиды"
        monkeypatch.setattr(db, "get_config_lookups",
                            lambda: ({"заявки": "лиды"}, set()))
        monkeypatch.setattr(db, "get_specialists_order", lambda: ["Эльдана"])
        r = client.get("/api/today")
        assert r.status_code == 200
        data = r.get_json()
        assert data["display_names"] == {"заявки": "лиды"}
        # Raw row still uses key "заявки"
        assert data["rows"][0]["metric"] == "заявки"


# ============================================================
# Scenario 3: Hide / restore metric
# ============================================================

class TestHideRestoreMetric:
    """Metrics can be soft-deleted; their old data remains visible in aggregation."""

    def test_inactive_metric_still_in_history(self):
        # User submitted "комментарий" for two days, then admin hid it.
        # Old data should still aggregate normally.
        rows = [
            row("2026-05-01", "S", "комментарий", "встреча с клиентом"),
            row("2026-05-02", "S", "комментарий", "подписали договор"),
        ]
        agg = bot.aggregate_reports(rows, text_keys={"комментарий"})
        assert len(agg["S"]["comments"]) == 2

    def test_hide_endpoint_marks_inactive(self, client, fake_conn, auth_headers):
        client.post("/admin/api/metric/5/delete", headers=auth_headers)
        sql = " ".join(q for q, _ in fake_conn.queries)
        assert "is_active = FALSE" in sql

    def test_restore_endpoint_brings_back(self, client, fake_conn, auth_headers):
        client.post("/admin/api/metric/5/restore", headers=auth_headers)
        sql = " ".join(q for q, _ in fake_conn.queries)
        assert "is_active = TRUE" in sql


# ============================================================
# Scenario 4: Onboarding new specialist
# ============================================================

class TestOnboardingScenario:
    """Boss adds new employee → he should appear on dashboard before any data."""

    def test_specialist_in_today_response_after_creation(self, client, monkeypatch, auth_headers):
        # After admin adds Виктор, get_specialists_order returns him
        monkeypatch.setattr(db, "get_today_reports", lambda: [])
        monkeypatch.setattr(db, "get_config_lookups", lambda: ({}, set()))
        monkeypatch.setattr(db, "get_specialists_order",
                            lambda: ["Эльдана", "Виктор"])
        r = client.get("/api/today")
        data = r.get_json()
        assert "Виктор" in data["specialists_order"]


# ============================================================
# Scenario 5: Reminder time change
# ============================================================

class TestReminderTimeChange:
    def test_invalid_time_doesnt_save(self, client, monkeypatch, auth_headers):
        captured = {}
        monkeypatch.setattr(db, "set_setting",
                            lambda k, v: captured.update({k: v}))
        client.post(
            "/admin/api/settings",
            json={"reminder_time": "abracadabra"},
            headers=auth_headers,
        )
        assert captured == {}  # nothing was saved

    def test_valid_time_persists(self, client, monkeypatch, auth_headers):
        captured = {}
        monkeypatch.setattr(db, "set_setting",
                            lambda k, v: captured.update({k: v}))
        r = client.post(
            "/admin/api/settings",
            json={"reminder_time": "09:15"},
            headers=auth_headers,
        )
        assert r.status_code == 200
        assert captured["reminder_time"] == "09:15"


# ============================================================
# Scenario 6: Aggregate API includes plans data
# ============================================================

class TestAggregateApiPlans:
    def test_plans_included_for_active_period(self, client, monkeypatch, auth_headers):
        """When a metric has plan_week set, /api/aggregate?period=week
        should return it nested under specialists in `plans`."""
        monkeypatch.setattr(db, "get_period_reports", lambda s, e: [])
        monkeypatch.setattr(db, "get_config_lookups", lambda: ({}, set()))
        monkeypatch.setattr(db, "get_specialists_order", lambda: ["S"])
        monkeypatch.setattr(db, "get_plans", lambda: {
            ("S", "контакты"): {"week": 50.0, "month": 200.0},
            ("S", "доход"):    {"week": None, "month": 1_000_000.0},
        })
        r = client.get("/api/aggregate?period=week")
        data = r.get_json()
        assert data["plans"] == {"S": {"контакты": 50.0}}
        # доход has no week plan → not in week response

    def test_month_plans_returned_for_month_period(self, client, monkeypatch, auth_headers):
        monkeypatch.setattr(db, "get_period_reports", lambda s, e: [])
        monkeypatch.setattr(db, "get_config_lookups", lambda: ({}, set()))
        monkeypatch.setattr(db, "get_specialists_order", lambda: ["S"])
        monkeypatch.setattr(db, "get_plans", lambda: {
            ("S", "доход"): {"week": None, "month": 1_000_000.0},
        })
        r = client.get("/api/aggregate?period=month")
        data = r.get_json()
        assert data["plans"] == {"S": {"доход": 1_000_000.0}}


# ============================================================
# Scenario 7: Holiday excludes reminder
# ============================================================

class TestHolidayExclusion:
    def test_holiday_added_to_set_excludes_that_day(self, monkeypatch):
        from datetime import date
        # 2026-05-04 is Monday — normally a working day
        assert bot.is_working_day(date(2026, 5, 4)) is True
        # Add it to HOLIDAYS — now non-working
        monkeypatch.setattr(config, "HOLIDAYS", {"2026-05-04"})
        assert bot.is_working_day(date(2026, 5, 4)) is False
        # Adjacent days still working
        assert bot.is_working_day(date(2026, 5, 5)) is True
