"""Tests for /admin and /admin/api/* endpoints.

Strategy:
- Auth tests: real Flask test_client + mocked psycopg2 conn (so no DB needed).
- Endpoint tests: patch helper functions to inject controllable responses;
  inspect HTTP status / JSON shape / DB-write side effects via RecordingConn.queries.

Patching note: web_admin.py calls `db.get_X()` and reads `config.ADMIN_TOKEN`
directly, so monkeypatches must target those modules (not `bot`).
"""
import json
import pytest

import bot
import config
import db
import tg_bot


# ============================================================
# Authentication & token flow
# ============================================================

class TestAdminAuth:
    def test_admin_page_renders_when_token_set(self, client):
        r = client.get("/admin")
        assert r.status_code == 200
        # Title contains the company name in Russian
        assert "Каравелла" in r.data.decode("utf-8")

    def test_admin_page_returns_503_when_token_unset(self, client, monkeypatch):
        monkeypatch.setattr(config, "ADMIN_TOKEN", "")
        r = client.get("/admin")
        assert r.status_code == 503

    def test_api_rejects_no_token(self, client, fake_conn):
        r = client.get("/admin/api/config")
        assert r.status_code == 401

    def test_api_rejects_wrong_token_via_query(self, client, fake_conn):
        r = client.get("/admin/api/config?token=wrong")
        assert r.status_code == 401

    def test_api_rejects_wrong_token_via_header(self, client, fake_conn):
        r = client.get("/admin/api/config", headers={"X-Admin-Token": "wrong"})
        assert r.status_code == 401

    def test_api_accepts_query_token(self, client, fake_conn):
        r = client.get("/admin/api/config?token=testtoken")
        assert r.status_code == 200

    def test_api_accepts_header_token(self, client, fake_conn, auth_headers):
        r = client.get("/admin/api/config", headers=auth_headers)
        assert r.status_code == 200

    def test_api_503_when_admin_token_unset(self, client, monkeypatch):
        monkeypatch.setattr(config, "ADMIN_TOKEN", "")
        r = client.get("/admin/api/config?token=anything")
        assert r.status_code == 503


# ============================================================
# /admin/api/config — read endpoint
# ============================================================

class TestAdminConfig:
    def test_returns_specialists_groups_and_metrics(self, client, monkeypatch, auth_headers):
        monkeypatch.setattr(db, "get_admin_config", lambda: [
            {"id": 1, "specialist": "Эльдана", "metric_key": "заявки",
             "display_name": "заявки", "question_text": "📥 …",
             "position": 0, "is_text": False, "is_active": True,
             "plan_week": None, "plan_month": None}
        ])
        monkeypatch.setattr(db, "get_admin_specialist_groups", lambda: [
            {"name": "Эльдана", "is_active": True}
        ])
        r = client.get("/admin/api/config", headers=auth_headers)
        assert r.status_code == 200
        data = r.get_json()
        assert data["specialists"] == ["Эльдана"]
        assert data["specialist_groups"] == [{"name": "Эльдана", "is_active": True}]
        assert len(data["metrics"]) == 1
        assert data["metrics"][0]["display_name"] == "заявки"

    def test_inactive_specialist_appears_in_groups(self, client, monkeypatch, auth_headers):
        """A2 fix: hidden specialists must still show in admin config."""
        monkeypatch.setattr(db, "get_admin_config", lambda: [])
        monkeypatch.setattr(db, "get_admin_specialist_groups", lambda: [
            {"name": "Активный", "is_active": True},
            {"name": "Скрытый",  "is_active": False},
        ])
        r = client.get("/admin/api/config", headers=auth_headers)
        groups = r.get_json()["specialist_groups"]
        assert len(groups) == 2
        assert any(g["name"] == "Скрытый" and not g["is_active"] for g in groups)

    def test_falls_back_to_default_order_when_empty(self, client, monkeypatch, auth_headers):
        monkeypatch.setattr(db, "get_admin_config", lambda: [])
        monkeypatch.setattr(db, "get_admin_specialist_groups", lambda: [])
        r = client.get("/admin/api/config", headers=auth_headers)
        groups = r.get_json()["specialist_groups"]
        assert len(groups) == len(bot.DEFAULT_SPECIALIST_ORDER)


# ============================================================
# /admin/api/metric — create
# ============================================================

class TestMetricCreate:
    def test_creates_metric_with_valid_input(self, client, fake_conn, monkeypatch, auth_headers):
        monkeypatch.setattr(db, "get_specialists_order", lambda: ["Эльдана"])
        # Position max+1 query; gen_metric_key collision check; INSERT … RETURNING id
        fake_conn.fetchone_returns = [(0,), None, (42,)]
        r = client.post(
            "/admin/api/metric",
            json={"specialist": "Эльдана", "display": "лиды",
                  "question": "📥 Сколько лидов?", "is_text": False},
            headers=auth_headers,
        )
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["id"] == 42
        assert data["metric_key"] == "лиды"

    def test_rejects_unknown_specialist(self, client, monkeypatch, auth_headers):
        monkeypatch.setattr(db, "get_specialists_order", lambda: ["Только_Эльдана"])
        r = client.post(
            "/admin/api/metric",
            json={"specialist": "Несуществующий", "display": "x", "question": "y"},
            headers=auth_headers,
        )
        assert r.status_code == 400

    def test_rejects_empty_display(self, client, fake_conn, monkeypatch, auth_headers):
        monkeypatch.setattr(db, "get_specialists_order", lambda: ["Эльдана"])
        r = client.post(
            "/admin/api/metric",
            json={"specialist": "Эльдана", "display": "", "question": "y"},
            headers=auth_headers,
        )
        assert r.status_code == 400

    def test_rejects_missing_question(self, client, fake_conn, monkeypatch, auth_headers):
        monkeypatch.setattr(db, "get_specialists_order", lambda: ["Эльдана"])
        r = client.post(
            "/admin/api/metric",
            json={"specialist": "Эльдана", "display": "лиды"},
            headers=auth_headers,
        )
        assert r.status_code == 400


# ============================================================
# /admin/api/metric/<id>/update — including plans
# ============================================================

class TestMetricUpdate:
    def test_update_display(self, client, fake_conn, auth_headers):
        r = client.post(
            "/admin/api/metric/1/update",
            json={"display": "новое имя"},
            headers=auth_headers,
        )
        assert r.status_code == 200
        assert r.get_json() == {"ok": True}
        # Verify SQL contained UPDATE
        sql = " ".join(q for q, _ in fake_conn.queries)
        assert "UPDATE metrics_config SET display_name" in sql

    def test_update_plan_week_valid_number(self, client, fake_conn, auth_headers):
        r = client.post(
            "/admin/api/metric/1/update",
            json={"plan_week": 50},
            headers=auth_headers,
        )
        assert r.status_code == 200
        # The number 50 should be in the params
        update_params = [p for q, p in fake_conn.queries if "UPDATE" in q.upper()]
        assert update_params
        assert 50.0 in update_params[0]

    def test_update_plan_clears_with_empty_string(self, client, fake_conn, auth_headers):
        """User clears plan field — value becomes NULL in DB."""
        r = client.post(
            "/admin/api/metric/1/update",
            json={"plan_week": ""},
            headers=auth_headers,
        )
        assert r.status_code == 200
        update_params = [p for q, p in fake_conn.queries if "UPDATE" in q.upper()]
        assert None in update_params[0]

    def test_update_plan_clears_with_none(self, client, fake_conn, auth_headers):
        r = client.post(
            "/admin/api/metric/1/update",
            json={"plan_week": None},
            headers=auth_headers,
        )
        assert r.status_code == 200

    def test_update_plan_invalid_value_returns_400(self, client, fake_conn, auth_headers):
        r = client.post(
            "/admin/api/metric/1/update",
            json={"plan_week": "много"},
            headers=auth_headers,
        )
        assert r.status_code == 400

    def test_update_with_no_fields_returns_400(self, client, fake_conn, auth_headers):
        r = client.post(
            "/admin/api/metric/1/update", json={}, headers=auth_headers,
        )
        assert r.status_code == 400

    def test_update_is_text_toggle(self, client, fake_conn, auth_headers):
        r = client.post(
            "/admin/api/metric/1/update",
            json={"is_text": True},
            headers=auth_headers,
        )
        assert r.status_code == 200


# ============================================================
# Delete / restore / move
# ============================================================

class TestMetricSoftDelete:
    def test_delete_sets_inactive(self, client, fake_conn, auth_headers):
        r = client.post("/admin/api/metric/5/delete", headers=auth_headers)
        assert r.status_code == 200
        sql = " ".join(q for q, _ in fake_conn.queries)
        assert "is_active = FALSE" in sql

    def test_restore_sets_active(self, client, fake_conn, auth_headers):
        r = client.post("/admin/api/metric/5/restore", headers=auth_headers)
        assert r.status_code == 200
        sql = " ".join(q for q, _ in fake_conn.queries)
        assert "is_active = TRUE" in sql


class TestMetricMove:
    def test_move_invalid_direction(self, client, fake_conn, auth_headers):
        r = client.post(
            "/admin/api/metric/1/move",
            json={"direction": "sideways"},
            headers=auth_headers,
        )
        assert r.status_code == 400

    def test_move_when_metric_at_edge(self, client, fake_conn, auth_headers):
        # First UPDATE pulls (specialist,) tuple, then list of ids
        fake_conn.fetchone_returns = [("Эльдана",)]
        fake_conn.fetchall_returns = [[(1,), (2,), (3,)]]  # ids
        r = client.post(
            "/admin/api/metric/1/move",  # already first
            json={"direction": "up"},
            headers=auth_headers,
        )
        assert r.status_code == 200
        assert r.get_json()["ok"] is False

    def test_move_swap_positions(self, client, fake_conn, auth_headers):
        fake_conn.fetchone_returns = [("Эльдана",)]
        fake_conn.fetchall_returns = [[(10,), (20,), (30,)]]
        r = client.post(
            "/admin/api/metric/20/move",
            json={"direction": "up"},
            headers=auth_headers,
        )
        assert r.status_code == 200
        assert r.get_json() == {"ok": True}


# ============================================================
# Specialists CRUD
# ============================================================

class TestSpecialistsAdmin:
    def test_list_specialists(self, client, monkeypatch, auth_headers):
        monkeypatch.setattr(db, "get_specialists_admin", lambda: [
            {"id": 1, "name": "Эльдана", "telegram_id": "111",
             "position": 0, "is_active": True}
        ])
        r = client.get("/admin/api/specialists", headers=auth_headers)
        assert r.status_code == 200
        data = r.get_json()
        assert data["specialists"][0]["name"] == "Эльдана"

    def test_create_specialist(self, client, fake_conn, auth_headers):
        # COALESCE(MAX(...)) returns next position; INSERT RETURNING id
        fake_conn.fetchone_returns = [(7,), (99,)]
        r = client.post(
            "/admin/api/specialist",
            json={"name": "Виктор", "telegram_id": "555"},
            headers=auth_headers,
        )
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["id"] == 99

    def test_create_specialist_rejects_empty_name(self, client, fake_conn, auth_headers):
        r = client.post(
            "/admin/api/specialist",
            json={"name": "", "telegram_id": "555"},
            headers=auth_headers,
        )
        assert r.status_code == 400

    def test_rename_specialist_cascades_to_reports(self, client, fake_conn, auth_headers):
        """Verify reports.specialist + metrics_config.specialist UPDATEs are issued."""
        fake_conn.fetchone_returns = [("Старое имя",)]
        r = client.post(
            "/admin/api/specialist/1/update",
            json={"name": "Новое имя"},
            headers=auth_headers,
        )
        assert r.status_code == 200
        sqls = [q for q, _ in fake_conn.queries]
        assert any("UPDATE reports SET specialist" in q for q in sqls)
        assert any("UPDATE metrics_config SET specialist" in q for q in sqls)
        assert any("UPDATE specialists SET" in q for q in sqls)

    def test_rename_to_same_name_skips_cascade(self, client, fake_conn, auth_headers):
        """No cascade if name unchanged — performance optimization."""
        fake_conn.fetchone_returns = [("Эльдана",)]
        r = client.post(
            "/admin/api/specialist/1/update",
            json={"name": "Эльдана"},  # same name
            headers=auth_headers,
        )
        assert r.status_code == 200
        sqls = [q for q, _ in fake_conn.queries]
        # No cascade UPDATE on reports
        assert not any("UPDATE reports SET specialist" in q for q in sqls)


# ============================================================
# Settings
# ============================================================

class TestSettings:
    def test_get_returns_reminder_time(self, client, monkeypatch, auth_headers):
        monkeypatch.setattr(db, "get_setting", lambda k, d=None: "09:30")
        r = client.get("/admin/api/settings", headers=auth_headers)
        assert r.status_code == 200
        assert r.get_json() == {"reminder_time": "09:30"}

    def test_set_valid_time(self, client, monkeypatch, auth_headers):
        captured = {}
        monkeypatch.setattr(db, "set_setting", lambda k, v: captured.update({k: v}))
        # TG_APP is None during tests → schedule_reminder is no-op for live reschedule
        r = client.post(
            "/admin/api/settings",
            json={"reminder_time": "10:30"},
            headers=auth_headers,
        )
        assert r.status_code == 200
        assert captured == {"reminder_time": "10:30"}

    def test_set_invalid_time_rejected(self, client, monkeypatch, auth_headers):
        monkeypatch.setattr(db, "set_setting", lambda k, v: None)
        r = client.post(
            "/admin/api/settings",
            json={"reminder_time": "25:99"},
            headers=auth_headers,
        )
        assert r.status_code == 400

    def test_set_empty_time_rejected(self, client, monkeypatch, auth_headers):
        monkeypatch.setattr(db, "set_setting", lambda k, v: None)
        r = client.post(
            "/admin/api/settings",
            json={"reminder_time": ""},
            headers=auth_headers,
        )
        assert r.status_code == 400


# ============================================================
# Send reminder (manual trigger)
# ============================================================

class TestSendReminder:
    def test_send_to_all(self, client, monkeypatch, mock_telegram_http, auth_headers):
        monkeypatch.setattr(db, "get_specialists_dict", lambda: {
            "111": "Эльдана",
            "222": "Станислав",
        })
        r = client.post(
            "/admin/api/send-reminder",
            json={"specialist": "all"},
            headers=auth_headers,
        )
        assert r.status_code == 200
        data = r.get_json()
        assert sorted(data["sent"]) == ["Станислав", "Эльдана"]
        assert data["failed"] == []
        # Two HTTP calls to Telegram, one per specialist
        assert len(mock_telegram_http) == 2

    def test_send_to_specific_specialist(self, client, monkeypatch, mock_telegram_http, auth_headers):
        monkeypatch.setattr(db, "get_specialists_dict", lambda: {
            "111": "Эльдана", "222": "Станислав",
        })
        r = client.post(
            "/admin/api/send-reminder",
            json={"specialist": "Эльдана"},
            headers=auth_headers,
        )
        assert r.status_code == 200
        assert r.get_json()["sent"] == ["Эльдана"]
        assert len(mock_telegram_http) == 1
        # Check the chat_id matches
        assert mock_telegram_http[0]["json"]["chat_id"] == "111"

    def test_send_to_unknown_specialist_rejected(self, client, monkeypatch, mock_telegram_http, auth_headers):
        monkeypatch.setattr(db, "get_specialists_dict", lambda: {"111": "Эльдана"})
        r = client.post(
            "/admin/api/send-reminder",
            json={"specialist": "Несуществующий"},
            headers=auth_headers,
        )
        assert r.status_code == 400
        assert mock_telegram_http == []

    def test_message_text_contains_specialist_name(self, client, monkeypatch, mock_telegram_http, auth_headers):
        monkeypatch.setattr(db, "get_specialists_dict", lambda: {"111": "Эльдана"})
        r = client.post(
            "/admin/api/send-reminder",
            json={"specialist": "Эльдана"},
            headers=auth_headers,
        )
        assert r.status_code == 200
        text = mock_telegram_http[0]["json"]["text"]
        assert "Эльдана" in text
        assert "/start" in text
