"""Admin web UI + JSON API.

All endpoints (except the HTML page itself) are token-protected via
`X-Admin-Token` header or `?token=` query param.
"""
import hmac
import logging

import psycopg2
from flask import abort, jsonify, render_template, request

import config
import db
import tg_bot
from helpers import parse_hhmm, parse_number
from web_app import app

logger = logging.getLogger(__name__)


# ============================================================
# Auth
# ============================================================

def _check_admin_token():
    if not config.ADMIN_TOKEN:
        abort(503, "Admin not configured")
    token = request.args.get("token") or request.headers.get("X-Admin-Token") or ""
    if not hmac.compare_digest(token, config.ADMIN_TOKEN):
        abort(401, "Invalid token")


def _gen_metric_key(specialist, display, cur):
    """Generate a unique stable internal key from display name."""
    base = (display or "metric").lower().strip().replace(" ", "_")[:40]
    if not base:
        base = "metric"
    candidate = base
    counter = 2
    while True:
        cur.execute(
            "SELECT 1 FROM metrics_config WHERE specialist=%s AND metric_key=%s",
            (specialist, candidate),
        )
        if not cur.fetchone():
            return candidate
        candidate = f"{base}_{counter}"
        counter += 1


# ============================================================
# Admin page
# ============================================================

@app.route("/admin")
def admin_page():
    if not config.ADMIN_TOKEN:
        return "Admin disabled. Set ADMIN_TOKEN env var on Railway and redeploy.", 503
    return render_template("admin.html")


# ============================================================
# Config (read)
# ============================================================

@app.route("/admin/api/config")
def admin_config():
    _check_admin_token()
    rows = db.get_admin_config()
    groups = db.get_admin_specialist_groups()
    if not groups:
        groups = [{"name": n, "is_active": True} for n in config.DEFAULT_SPECIALIST_ORDER]
    return jsonify({
        "specialists": [g["name"] for g in groups],  # backwards-compat
        "specialist_groups": groups,
        "metrics": rows,
    })


# ============================================================
# Metrics CRUD
# ============================================================

@app.route("/admin/api/metric", methods=["POST"])
def admin_metric_create():
    _check_admin_token()
    data = request.get_json(silent=True) or {}
    specialist = (data.get("specialist") or "").strip()
    display = (data.get("display") or "").strip()
    question = (data.get("question") or "").strip()
    is_text = bool(data.get("is_text"))

    valid_specialists = set(db.get_specialists_order()) | set(config.DEFAULT_SPECIALIST_ORDER)
    if specialist not in valid_specialists:
        abort(400, "Unknown specialist")
    if not display or not question:
        abort(400, "display and question are required")

    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM metrics_config WHERE specialist = %s",
                (specialist,),
            )
            new_pos = cur.fetchone()[0]
            key = _gen_metric_key(specialist, display, cur)
            cur.execute(
                "INSERT INTO metrics_config (specialist, metric_key, display_name, "
                "question_text, position, is_text, is_active) "
                "VALUES (%s,%s,%s,%s,%s,%s,TRUE) RETURNING id",
                (specialist, key, display, question, new_pos, is_text),
            )
            new_id = cur.fetchone()[0]
    return jsonify({"id": new_id, "metric_key": key, "ok": True})


@app.route("/admin/api/metric/<int:metric_id>/update", methods=["POST"])
def admin_metric_update(metric_id):
    _check_admin_token()
    data = request.get_json(silent=True) or {}
    fields = {}
    if "display" in data:
        v = (data["display"] or "").strip()
        if not v:
            abort(400, "display cannot be empty")
        fields["display_name"] = v
    if "question" in data:
        v = (data["question"] or "").strip()
        if not v:
            abort(400, "question cannot be empty")
        fields["question_text"] = v
    if "is_text" in data:
        fields["is_text"] = bool(data["is_text"])
    for plan_field in ("plan_week", "plan_month"):
        if plan_field in data:
            raw = data[plan_field]
            if raw is None or raw == "" or raw is False:
                fields[plan_field] = None
            else:
                num = parse_number(raw)
                if num is None:
                    abort(400, f"{plan_field} must be a number or empty")
                fields[plan_field] = num
    if not fields:
        abort(400, "no fields to update")

    sets = ", ".join(f"{k} = %s" for k in fields)
    params = list(fields.values()) + [metric_id]
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE metrics_config SET {sets} WHERE id = %s", params)
            ok = cur.rowcount > 0
    return jsonify({"ok": ok})


@app.route("/admin/api/metric/<int:metric_id>/delete", methods=["POST"])
def admin_metric_delete(metric_id):
    _check_admin_token()
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE metrics_config SET is_active = FALSE WHERE id = %s", (metric_id,))
            ok = cur.rowcount > 0
    return jsonify({"ok": ok})


@app.route("/admin/api/metric/<int:metric_id>/restore", methods=["POST"])
def admin_metric_restore(metric_id):
    _check_admin_token()
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE metrics_config SET is_active = TRUE WHERE id = %s", (metric_id,))
            ok = cur.rowcount > 0
    return jsonify({"ok": ok})


@app.route("/admin/api/metric/<int:metric_id>/move", methods=["POST"])
def admin_metric_move(metric_id):
    _check_admin_token()
    direction = (request.get_json(silent=True) or {}).get("direction")
    if direction not in ("up", "down"):
        abort(400, "direction must be 'up' or 'down'")
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT specialist FROM metrics_config WHERE id = %s", (metric_id,))
            row = cur.fetchone()
            if not row:
                abort(404, "not found")
            specialist = row[0]
            cur.execute(
                "SELECT id FROM metrics_config WHERE specialist = %s AND is_active = TRUE "
                "ORDER BY position ASC, id ASC",
                (specialist,),
            )
            ids = [r[0] for r in cur.fetchall()]
            if metric_id not in ids:
                return jsonify({"ok": False, "reason": "inactive metric cannot be moved"})
            idx = ids.index(metric_id)
            new_idx = idx - 1 if direction == "up" else idx + 1
            if new_idx < 0 or new_idx >= len(ids):
                return jsonify({"ok": False, "reason": "edge"})
            ids[idx], ids[new_idx] = ids[new_idx], ids[idx]
            for pos, mid in enumerate(ids):
                cur.execute("UPDATE metrics_config SET position = %s WHERE id = %s", (pos, mid))
    return jsonify({"ok": True})


# ============================================================
# Settings
# ============================================================

@app.route("/admin/api/settings")
def admin_settings_get():
    _check_admin_token()
    return jsonify({"reminder_time": db.get_setting("reminder_time", "09:00")})


@app.route("/admin/api/settings", methods=["POST"])
def admin_settings_set():
    _check_admin_token()
    data = request.get_json(silent=True) or {}
    if "reminder_time" in data:
        t = (data["reminder_time"] or "").strip()
        if not parse_hhmm(t):
            abort(400, "reminder_time must be HH:MM (00:00–23:59)")
        db.set_setting("reminder_time", t)
        if tg_bot.TG_APP is not None:
            try:
                tg_bot.schedule_reminder(tg_bot.TG_APP, t)
            except Exception as e:
                logger.error(f"Reschedule failed: {e}")
                return jsonify({
                    "ok": True,
                    "warning": "Saved, but live reschedule failed; restart to apply",
                })
    return jsonify({"ok": True})


# ============================================================
# Specialists CRUD
# ============================================================

@app.route("/admin/api/specialists")
def admin_specialists_list():
    _check_admin_token()
    return jsonify({"specialists": db.get_specialists_admin()})


@app.route("/admin/api/specialist", methods=["POST"])
def admin_specialist_create():
    _check_admin_token()
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    tg_id = (data.get("telegram_id") or "").strip()
    if not name or not tg_id:
        abort(400, "name and telegram_id are required")
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM specialists")
            new_pos = cur.fetchone()[0]
            try:
                cur.execute(
                    "INSERT INTO specialists (name, telegram_id, position) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    (name, tg_id, new_pos),
                )
                new_id = cur.fetchone()[0]
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                abort(400, "Specialist with this name already exists")
    return jsonify({"id": new_id, "ok": True})


@app.route("/admin/api/specialist/<int:sp_id>/update", methods=["POST"])
def admin_specialist_update(sp_id):
    _check_admin_token()
    data = request.get_json(silent=True) or {}
    fields = {}
    if "name" in data:
        v = (data["name"] or "").strip()
        if not v:
            abort(400, "name cannot be empty")
        fields["name"] = v
    if "telegram_id" in data:
        v = (data["telegram_id"] or "").strip()
        if not v:
            abort(400, "telegram_id cannot be empty")
        fields["telegram_id"] = v
    if not fields:
        abort(400, "no fields to update")

    with db.get_conn() as conn:
        with conn.cursor() as cur:
            if "name" in fields:
                cur.execute("SELECT name FROM specialists WHERE id = %s", (sp_id,))
                row = cur.fetchone()
                if not row:
                    abort(404, "not found")
                old_name = row[0]
                new_name = fields["name"]
                if old_name != new_name:
                    cur.execute(
                        "UPDATE reports SET specialist = %s WHERE specialist = %s",
                        (new_name, old_name),
                    )
                    cur.execute(
                        "UPDATE metrics_config SET specialist = %s WHERE specialist = %s",
                        (new_name, old_name),
                    )
            sets = ", ".join(f"{k} = %s" for k in fields)
            params = list(fields.values()) + [sp_id]
            try:
                cur.execute(f"UPDATE specialists SET {sets} WHERE id = %s", params)
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                abort(400, "another specialist already uses this name")
    return jsonify({"ok": True})


@app.route("/admin/api/specialist/<int:sp_id>/delete", methods=["POST"])
def admin_specialist_delete(sp_id):
    _check_admin_token()
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE specialists SET is_active = FALSE WHERE id = %s", (sp_id,))
            ok = cur.rowcount > 0
    return jsonify({"ok": ok})


@app.route("/admin/api/specialist/<int:sp_id>/restore", methods=["POST"])
def admin_specialist_restore(sp_id):
    _check_admin_token()
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE specialists SET is_active = TRUE WHERE id = %s", (sp_id,))
            ok = cur.rowcount > 0
    return jsonify({"ok": ok})


@app.route("/admin/api/specialist/<int:sp_id>/move", methods=["POST"])
def admin_specialist_move(sp_id):
    _check_admin_token()
    direction = (request.get_json(silent=True) or {}).get("direction")
    if direction not in ("up", "down"):
        abort(400, "direction must be 'up' or 'down'")
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM specialists WHERE is_active = TRUE "
                "ORDER BY position ASC, id ASC"
            )
            ids = [r[0] for r in cur.fetchall()]
            if sp_id not in ids:
                return jsonify({"ok": False, "reason": "inactive cannot be moved"})
            idx = ids.index(sp_id)
            new_idx = idx - 1 if direction == "up" else idx + 1
            if new_idx < 0 or new_idx >= len(ids):
                return jsonify({"ok": False, "reason": "edge"})
            ids[idx], ids[new_idx] = ids[new_idx], ids[idx]
            for pos, sid in enumerate(ids):
                cur.execute("UPDATE specialists SET position = %s WHERE id = %s", (pos, sid))
    return jsonify({"ok": True})


# ============================================================
# Manual reminder send
# ============================================================

@app.route("/admin/api/send-reminder", methods=["POST"])
def admin_send_reminder():
    _check_admin_token()
    data = request.get_json(silent=True) or {}
    target = (data.get("specialist") or "all").strip()
    sp_dict = db.get_specialists_dict()
    if target == "all":
        targets = list(sp_dict.items())
    else:
        match = [(tg, name) for tg, name in sp_dict.items() if name == target]
        if not match:
            abort(400, f"specialist '{target}' not found or inactive")
        targets = match

    sent = []
    failed = []
    for tg_id, name in targets:
        text = f"⏰ {name}, время отчёта!\nНапиши /start 👇"
        try:
            tg_bot.send_telegram_message(tg_id, text)
            sent.append(name)
            logger.info(f"Manual reminder sent to {name} ({tg_id})")
        except Exception as e:
            failed.append({"name": name, "error": str(e)})
            logger.error(f"Manual reminder failed for {name}: {e}")
    return jsonify({"ok": True, "sent": sent, "failed": failed})
