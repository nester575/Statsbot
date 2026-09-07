"""Tests for /api/aggregate?period=custom — arbitrary date range selection.

Feature: dashboard has a third tab «Период» where the user picks start/end
dates. Backend accepts period=custom&start=YYYY-MM-DD&end=YYYY-MM-DD and
aggregates the same way as week/month.
"""
import pytest

import db


class TestCustomPeriodBackend:
    def test_valid_custom_range_returns_correct_dates(self, client, monkeypatch):
        monkeypatch.setattr(db, "get_period_reports", lambda s, e: [])
        monkeypatch.setattr(db, "get_config_lookups", lambda: ({}, set()))
        monkeypatch.setattr(db, "get_specialists_order", lambda: [])
        monkeypatch.setattr(db, "get_plans", lambda: {})
        r = client.get("/api/aggregate?period=custom&start=2026-08-01&end=2026-08-15")
        assert r.status_code == 200
        d = r.get_json()
        assert d["period"] == "custom"
        assert d["start"] == "2026-08-01"
        assert d["end"] == "2026-08-15"

    def test_plans_excluded_for_custom_period(self, client, monkeypatch):
        """Plans are calibrated for week/month; for custom range they'd be
        misleading (a 3-day range vs weekly plan = always red)."""
        monkeypatch.setattr(db, "get_period_reports", lambda s, e: [])
        monkeypatch.setattr(db, "get_config_lookups", lambda: ({}, set()))
        monkeypatch.setattr(db, "get_specialists_order", lambda: ["Эльдана"])
        monkeypatch.setattr(db, "get_plans", lambda: {
            ("Эльдана", "заявки"): {"week": 50.0, "month": 200.0},
        })
        r = client.get("/api/aggregate?period=custom&start=2026-08-01&end=2026-08-10")
        assert r.get_json()["plans"] == {}

    def test_plans_still_included_for_week(self, client, monkeypatch):
        monkeypatch.setattr(db, "get_period_reports", lambda s, e: [])
        monkeypatch.setattr(db, "get_config_lookups", lambda: ({}, set()))
        monkeypatch.setattr(db, "get_specialists_order", lambda: ["Эльдана"])
        monkeypatch.setattr(db, "get_plans", lambda: {
            ("Эльдана", "заявки"): {"week": 50.0, "month": 200.0},
        })
        r = client.get("/api/aggregate?period=week")
        assert r.get_json()["plans"] == {"Эльдана": {"заявки": 50.0}}

    def test_prev_period_of_same_length(self, client, monkeypatch):
        """Trend uses equal-length previous window ending the day before start."""
        monkeypatch.setattr(db, "get_period_reports", lambda s, e: [])
        monkeypatch.setattr(db, "get_config_lookups", lambda: ({}, set()))
        monkeypatch.setattr(db, "get_specialists_order", lambda: [])
        monkeypatch.setattr(db, "get_plans", lambda: {})
        # 10-day custom range: 2026-08-01..2026-08-10 (10 days inclusive)
        # Expected prev: 2026-07-22..2026-07-31 (also 10 days)
        r = client.get("/api/aggregate?period=custom&start=2026-08-01&end=2026-08-10")
        d = r.get_json()
        assert d["prev_start"] == "2026-07-22"
        assert d["prev_end"] == "2026-07-31"


class TestCustomPeriodValidation:
    def test_rejects_missing_start(self, client):
        r = client.get("/api/aggregate?period=custom&end=2026-08-10")
        assert r.status_code == 400

    def test_rejects_missing_end(self, client):
        r = client.get("/api/aggregate?period=custom&start=2026-08-01")
        assert r.status_code == 400

    def test_rejects_bad_date_format(self, client):
        r = client.get("/api/aggregate?period=custom&start=01-08-2026&end=10-08-2026")
        assert r.status_code == 400

    def test_rejects_start_after_end(self, client):
        r = client.get("/api/aggregate?period=custom&start=2026-08-10&end=2026-08-01")
        assert r.status_code == 400

    def test_rejects_range_too_wide(self, client, monkeypatch):
        """Guard against DB abuse — 10-year request should 400."""
        r = client.get("/api/aggregate?period=custom&start=2020-01-01&end=2026-08-01")
        assert r.status_code == 400

    def test_accepts_single_day_range(self, client, monkeypatch):
        """Start == end is valid — one day's data."""
        monkeypatch.setattr(db, "get_period_reports", lambda s, e: [])
        monkeypatch.setattr(db, "get_config_lookups", lambda: ({}, set()))
        monkeypatch.setattr(db, "get_specialists_order", lambda: [])
        monkeypatch.setattr(db, "get_plans", lambda: {})
        r = client.get("/api/aggregate?period=custom&start=2026-08-01&end=2026-08-01")
        assert r.status_code == 200
        assert r.get_json()["start"] == r.get_json()["end"] == "2026-08-01"


class TestCustomPeriodUI:
    """Verify dashboard.html has the new UI elements wired up."""

    @pytest.fixture
    def html(self, client):
        return client.get("/").data.decode("utf-8")

    def test_custom_tab_present(self, html):
        assert 'data-tab="custom"' in html
        assert "Период" in html

    def test_date_inputs_present(self, html):
        assert 'id="customStart"' in html
        assert 'id="customEnd"' in html
        assert 'id="customApply"' in html

    def test_quick_range_buttons_present(self, html):
        """Quick presets for 7/14/30/90 days."""
        assert 'data-days="7"' in html
        assert 'data-days="14"' in html
        assert 'data-days="30"' in html
        assert 'data-days="90"' in html

    def test_custom_view_container_present(self, html):
        assert 'id="custom"' in html
        assert 'id="custom-data"' in html
        assert 'id="custom-period"' in html

    def test_init_function_present(self, html):
        """The one-time init that sets max=today, defaults, event listeners."""
        assert "initCustomPickerIfNeeded" in html
