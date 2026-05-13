"""Tests for the /edit feature: audit-aware upsert + admin audit endpoint."""
import json

import db


# ============================================================
# DB-level: upsert_report_with_audit diffing
# ============================================================

class TestUpsertWithAudit:
    """SQL-level invariants for the audit-aware upsert."""

    def test_preserves_original_time_when_existing_report(self, fake_conn):
        """If there's already a report for the day, the new rows must keep
        that report's original `time` (not jump to 18:00)."""
        # Existing row: метрика "заявки" = "5", time = "11:23:00"
        fake_conn.fetchall_returns = [
            [("заявки", "5", "11:23:00")],
        ]
        changes, time_used = db.upsert_report_with_audit(
            "Эльдана", "2026-05-12", {"заявки": "9"},
            via="bot", by="123",
        )
        assert time_used == "11:23:00"
        # Reports INSERT should have time=11:23:00
        report_inserts = [(q, p) for q, p in fake_conn.queries
                          if "INSERT INTO reports" in q]
        assert len(report_inserts) == 1
        assert "11:23:00" in str(report_inserts[0][1])
        assert changes == 1  # one metric changed

    def test_falls_back_to_18_00_when_no_existing(self, fake_conn):
        """No prior report → default time 18:00:00 (matches admin retro convention)."""
        fake_conn.fetchall_returns = [[]]  # no existing rows
        changes, time_used = db.upsert_report_with_audit(
            "Эльдана", "2026-05-12", {"заявки": "9"},
            via="admin",
        )
        assert time_used == "18:00:00"
        report_inserts = [(q, p) for q, p in fake_conn.queries
                          if "INSERT INTO reports" in q]
        assert "18:00:00" in str(report_inserts[0][1])

    def test_unchanged_values_produce_no_audit_rows(self, fake_conn):
        """If the new value matches the old, no INSERT into report_edits."""
        fake_conn.fetchall_returns = [
            [("заявки", "5", "10:00:00")],
        ]
        changes, _ = db.upsert_report_with_audit(
            "Эльдана", "2026-05-12", {"заявки": "5"},  # same value
            via="bot", by="123",
        )
        assert changes == 0
        audit_inserts = [q for q, _ in fake_conn.queries
                         if "INSERT INTO report_edits" in q]
        assert len(audit_inserts) == 0

    def test_changed_value_logs_old_and_new(self, fake_conn):
        fake_conn.fetchall_returns = [
            [("заявки", "5", "10:00:00")],
        ]
        changes, _ = db.upsert_report_with_audit(
            "Эльдана", "2026-05-12", {"заявки": "9"},
            via="bot", by="123",
        )
        assert changes == 1
        audit_inserts = [(q, p) for q, p in fake_conn.queries
                         if "INSERT INTO report_edits" in q]
        assert len(audit_inserts) == 1
        # Params include old="5" and new="9"
        params = audit_inserts[0][1]
        assert "5" in str(params)
        assert "9" in str(params)
        assert "bot" in str(params)
        assert "123" in str(params)

    def test_new_metric_logs_old_none(self, fake_conn):
        """Adding a metric that wasn't there before: old=NULL, new=value."""
        fake_conn.fetchall_returns = [
            [("заявки", "5", "10:00:00")],
        ]
        changes, _ = db.upsert_report_with_audit(
            "Эльдана", "2026-05-12",
            {"заявки": "5", "письма": "3"},  # adds письма
            via="bot",
        )
        assert changes == 1  # only письма new
        audit_inserts = [p for q, p in fake_conn.queries
                         if "INSERT INTO report_edits" in q]
        assert len(audit_inserts) == 1
        params = audit_inserts[0]
        # old_value should be None for new metric
        assert None in params
        assert "письма" in str(params)
        assert "3" in str(params)

    def test_removed_metric_logs_new_none(self, fake_conn):
        """Dropping a metric: old=value, new=NULL."""
        fake_conn.fetchall_returns = [
            [("заявки", "5", "10:00:00"), ("письма", "3", "10:00:00")],
        ]
        changes, _ = db.upsert_report_with_audit(
            "Эльдана", "2026-05-12",
            {"заявки": "5"},  # письма dropped
            via="bot",
        )
        assert changes == 1
        audit_inserts = [p for q, p in fake_conn.queries
                         if "INSERT INTO report_edits" in q]
        assert len(audit_inserts) == 1
        # The dropped metric is письма; new_value should be None
        params = audit_inserts[0]
        assert "письма" in str(params)
        # Find the new_value position (5th in INSERT tuple after report_date,
        # specialist, metric, old_value)
        assert params[4] is None

    def test_via_admin_uses_no_editor_id(self, fake_conn):
        """Admin edits don't have a telegram_id; `by` should be None."""
        fake_conn.fetchall_returns = [
            [("заявки", "5", "10:00:00")],
        ]
        db.upsert_report_with_audit(
            "Эльдана", "2026-05-12", {"заявки": "9"},
            via="admin",
        )
        audit_inserts = [p for q, p in fake_conn.queries
                         if "INSERT INTO report_edits" in q]
        params = audit_inserts[0]
        # Last param is `edited_by` — should be None for admin
        assert params[-1] is None
        assert "admin" in str(params)


# ============================================================
# DB-level: get_recent_edits ordering + limit
# ============================================================

class TestGetRecentEdits:
    def test_default_limit_used(self, fake_conn):
        fake_conn.fetchall_returns = [[]]
        db.get_recent_edits()
        last_query = fake_conn.queries[-1]
        assert "ORDER BY edited_at DESC" in last_query[0]
        assert last_query[1] == (200,)

    def test_custom_limit_passed_through(self, fake_conn):
        fake_conn.fetchall_returns = [[]]
        db.get_recent_edits(limit=50)
        assert fake_conn.queries[-1][1] == (50,)


# ============================================================
# DB-level: delete logs each removed metric
# ============================================================

class TestDeleteWithAudit:
    def test_delete_logs_each_metric(self, fake_conn):
        fake_conn.fetchall_returns = [
            [("заявки", "5"), ("письма", "3")],
        ]
        fake_conn.rowcount_default = 2
        n = db.delete_report_for_day("Эльдана", "2026-05-12", via="admin")
        assert n == 2
        audit_inserts = [q for q, _ in fake_conn.queries
                         if "INSERT INTO report_edits" in q]
        assert len(audit_inserts) == 2

    def test_delete_with_no_rows_no_audit(self, fake_conn):
        fake_conn.fetchall_returns = [[]]
        fake_conn.rowcount_default = 0
        db.delete_report_for_day("Эльдана", "2026-05-12", via="admin")
        audit_inserts = [q for q, _ in fake_conn.queries
                         if "INSERT INTO report_edits" in q]
        assert len(audit_inserts) == 0


# ============================================================
# HTTP-level: /admin/api/edits
# ============================================================

class TestAdminEditsEndpoint:
    def test_requires_token(self, client, fake_conn):
        r = client.get("/admin/api/edits")
        assert r.status_code == 401

    def test_returns_edits_with_valid_token(self, client, fake_conn, auth_headers):
        fake_conn.fetchall_returns = [[
            {
                "id": 1,
                "edited_at": "2026-05-12 14:30:00",
                "report_date": "2026-05-12",
                "specialist": "Эльдана",
                "metric": "заявки",
                "old_value": "5",
                "new_value": "9",
                "edited_via": "bot",
                "edited_by": "123",
            }
        ]]
        r = client.get("/admin/api/edits", headers=auth_headers)
        assert r.status_code == 200
        data = r.get_json()
        assert data["count"] == 1
        assert data["edits"][0]["metric"] == "заявки"
        assert data["edits"][0]["edited_via"] == "bot"

    def test_limit_clamped_to_max(self, client, fake_conn, auth_headers):
        """Even if user passes limit=99999, server caps it at 500."""
        fake_conn.fetchall_returns = [[]]
        client.get("/admin/api/edits?limit=99999", headers=auth_headers)
        last_query = fake_conn.queries[-1]
        # The LIMIT param passed to SQL should be 500, not 99999
        assert last_query[1] == (500,)

    def test_limit_clamped_to_min(self, client, fake_conn, auth_headers):
        fake_conn.fetchall_returns = [[]]
        client.get("/admin/api/edits?limit=0", headers=auth_headers)
        assert fake_conn.queries[-1][1] == (1,)

    def test_invalid_limit_falls_back_to_default(self, client, fake_conn, auth_headers):
        fake_conn.fetchall_returns = [[]]
        client.get("/admin/api/edits?limit=abc", headers=auth_headers)
        assert fake_conn.queries[-1][1] == (100,)

    def test_token_via_query_param_works(self, client, fake_conn, auth_query):
        fake_conn.fetchall_returns = [[]]
        r = client.get("/admin/api/edits" + auth_query)
        assert r.status_code == 200


# ============================================================
# HTTP-level: retro entry now writes audit
# ============================================================

class TestRetroAuditWiring:
    def test_admin_retro_post_writes_via_admin(self, client, fake_conn, auth_headers):
        """The /admin/api/report POST should call audit-aware upsert
        which writes INSERT INTO report_edits with via='admin'."""
        # Mock: no existing rows for the date
        fake_conn.fetchall_returns = [[]]
        r = client.post(
            "/admin/api/report",
            json={"specialist": "Эльдана", "date": "2026-05-12",
                  "values": {"заявки": "10"}},
            headers=auth_headers,
        )
        assert r.status_code == 200
        # Should have INSERT into report_edits with 'admin' as via
        audit_inserts = [(q, p) for q, p in fake_conn.queries
                         if "INSERT INTO report_edits" in q]
        assert len(audit_inserts) == 1
        assert "admin" in str(audit_inserts[0][1])


# ============================================================
# Smoke: re-exports from bot module
# ============================================================

class TestBotReExports:
    """The bot module must expose new symbols (for tests + future imports)."""

    def test_edit_states_exported(self):
        import bot
        assert bot.EDIT_PICK_DATE == 2
        assert bot.EDIT_ASKING == 3

    def test_edit_handlers_exported(self):
        import bot
        assert callable(bot.edit_start)
        assert callable(bot.edit_picked_date)
        assert callable(bot.edit_answer)
        assert callable(bot.edit_cancel)

    def test_audit_db_functions_exported(self):
        import bot
        assert callable(bot.upsert_report_with_audit)
        assert callable(bot.get_recent_edits)

    def test_edit_sessions_dict_exported(self):
        import bot
        assert isinstance(bot.edit_sessions, dict)
