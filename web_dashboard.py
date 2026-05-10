"""Dashboard + public read-only API endpoints.

Importing this module registers routes on the shared `app`.
"""
from datetime import timedelta

from flask import jsonify, render_template, request

import db
import helpers
from aggregate import aggregate_reports
from web_app import app


@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/today")
def api_today():
    rows = db.get_today_reports()
    display, text_keys = db.get_config_lookups()
    return jsonify({
        "rows": [dict(r) for r in rows],
        "display_names": display,
        "text_keys": list(text_keys),
        "specialists_order": db.get_specialists_order(),
    })


@app.route("/api/aggregate")
def api_aggregate():
    period = request.args.get("period", "week")
    if period not in ("week", "month"):
        period = "week"
    start, end = helpers.period_range(period)
    display, text_keys = db.get_config_lookups()

    rows = db.get_period_reports(start, end)
    data = aggregate_reports(rows, text_keys)

    # Equal-length previous period for trend comparison
    length = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=length - 1)
    prev_rows = db.get_period_reports(prev_start, prev_end)
    prev_data = aggregate_reports(prev_rows, text_keys)
    prev_metrics = {sp: b["metrics"] for sp, b in prev_data.items()}

    # Plans relevant to this period only
    plans = db.get_plans()
    plans_out = {}
    for (sp, key), p in plans.items():
        v = p.get(period)
        if v is not None:
            plans_out.setdefault(sp, {})[key] = v

    return jsonify({
        "period": period,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "prev_start": prev_start.isoformat(),
        "prev_end": prev_end.isoformat(),
        "specialists": data,
        "prev_specialists": prev_metrics,
        "display_names": display,
        "text_keys": list(text_keys),
        "specialists_order": db.get_specialists_order(),
        "plans": plans_out,
    })


@app.route("/health")
def health():
    return "ok"
