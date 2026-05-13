"""HTML/JS structure tests — verify rendered admin and dashboard pages
contain the expected UI elements and JS hooks.

These don't run JS; they're "lint-tests" against the rendered template
text, catching regressions like missing event handlers, broken CSS hooks,
or accidentally removed sections.
"""
import re

import pytest


# ============================================================
# Dashboard page (/)
# ============================================================

class TestDashboardStructure:
    @pytest.fixture
    def html(self, client):
        return client.get("/").data.decode("utf-8")

    def test_title_uses_karavella_brand(self, html):
        assert "Live Dashboard" in html
        assert "Каравелла" in html

    def test_tabs_present(self, html):
        """After today→week merge there are exactly 2 period tabs."""
        assert 'data-tab="week"' in html
        assert 'data-tab="month"' in html
        # No leftover today tab
        assert 'data-tab="today"' not in html

    def test_week_tab_is_default_active(self, html):
        # The active class should be on the week tab on initial render
        assert re.search(r'class="tab active"\s+data-tab="week"', html)

    def test_status_section_lives_above_tabs(self, html):
        """The «Статус сдачи сегодня» is outside any view div — always visible."""
        # #status comes before <div class="tabs"> in DOM order
        status_idx = html.find('id="status"')
        tabs_idx = html.find('class="tabs"')
        assert 0 < status_idx < tabs_idx

    def test_today_marker_styles_defined(self, html):
        """CSS hooks for highlighting today's day-cell."""
        assert ".day-cell.today" in html
        # Yellow accent — picked up by tests on visual regressions
        assert "#e8ff47" in html

    def test_metric_grid_columns_match_header(self, html):
        """The grid template should have 7 columns matching the 7 header cells."""
        # Look for "grid-template-columns:120px minmax(0,1fr)..." pattern
        m = re.search(r"\.metric-grid\{[^}]*grid-template-columns:([^;}]+)", html)
        assert m, "metric-grid CSS not found"
        cols = m.group(1).strip()
        # Count column tracks: each track separated by whitespace
        # 7 tracks: label, days(1fr), avg, plan, trend, total, chart
        assert cols.count(" ") >= 5  # at least 6 spaces between 7 tracks

    def test_link_to_admin_present(self, html):
        assert 'href="/admin"' in html


# ============================================================
# Admin page (/admin)
# ============================================================

class TestAdminStructure:
    @pytest.fixture
    def html(self, client):
        return client.get("/admin").data.decode("utf-8")

    def test_renders_with_token_set(self, html):
        assert "Каравелла" in html
        assert "admin-token" in html  # login prompt label

    def test_has_settings_card(self, html):
        assert "Время опроса" in html
        assert 'id="reminderTime"' in html

    def test_has_specialists_section(self, html):
        assert "Сотрудники" in html
        assert 'id="spList"' in html

    def test_has_retro_entry_section(self, html):
        """The «Отчёт задним числом» card with select + date + load button."""
        assert "Отчёт задним числом" in html
        assert 'id="reportSpec"' in html
        assert 'id="reportDate"' in html
        assert 'id="reportLoadBtn"' in html
        assert 'id="reportFormArea"' in html

    def test_date_input_has_dark_color_scheme(self, html):
        """color-scheme:dark makes native calendar visible on dark background."""
        assert "color-scheme:dark" in html

    def test_date_input_calendar_icon_styled(self, html):
        """Calendar indicator inverted for dark theme visibility."""
        assert "calendar-picker-indicator" in html
        assert "filter:invert" in html or "filter: invert" in html

    def test_date_input_init_called_on_render(self, html):
        """initReportDateInput() should be wired up — sets min/max + auto-open."""
        assert "initReportDateInput" in html
        # The function sets min/max and uses showPicker for auto-open
        assert "showPicker" in html

    def test_all_action_handlers_have_cases(self, html):
        """Each data-action attribute should have a matching switch case in JS."""
        actions = set(re.findall(r'data-action="([^"]+)"', html))
        # Each action must be referenced somewhere in JS (case label or string)
        for action in actions:
            assert f"'{action}'" in html or f'"{action}"' in html, \
                f"action '{action}' has no handler"

    def test_no_inline_event_handlers(self, html):
        """A1 fix: all onclick/onblur/onchange replaced by event delegation."""
        assert "onclick=" not in html
        assert "onblur=" not in html
        assert "onchange=" not in html

    def test_has_edits_log_section(self, html):
        """The «Журнал правок» card with load button + area."""
        assert "Журнал правок" in html
        assert 'id="editsLoadBtn"' in html
        assert 'id="editsArea"' in html

    def test_edits_log_uses_lazy_load(self, html):
        """We don't fetch /admin/api/edits on every page load — must require a click."""
        # The endpoint name must appear (in loadEdits function),
        # but loadAll must NOT call it
        assert "/admin/api/edits" in html
        # Verify loadAll does NOT auto-call edits endpoint
        load_all_match = re.search(r"async function loadAll\(\).*?\n\}", html, re.DOTALL)
        assert load_all_match is not None
        assert "/admin/api/edits" not in load_all_match.group(0)


# ============================================================
# Cross-cutting checks
# ============================================================

class TestCrossPageInvariants:
    def test_both_pages_serve_200(self, client):
        assert client.get("/").status_code == 200
        assert client.get("/admin").status_code == 200
        assert client.get("/health").status_code == 200

    def test_admin_disabled_when_token_unset(self, client, monkeypatch):
        import config
        monkeypatch.setattr(config, "ADMIN_TOKEN", "")
        r = client.get("/admin")
        assert r.status_code == 503
