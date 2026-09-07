"""Dashboard + public read-only API endpoints.

Importing this module registers routes on the shared `app`.
"""
from datetime import datetime, timedelta

from flask import abort, jsonify, render_template, request

import db
import helpers
from aggregate import aggregate_reports
from web_app import app


# Hard cap for custom period: prevents someone from asking for 10 years of data
# and hammering the DB. A year is more than enough for any real dashboard use.
MAX_CUSTOM_RANGE_DAYS = 366


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


def _resolve_period():
    """Return (period_label, start_date, end_date) from request args.

    Supported:
      - period=week  → last 7 calendar days
      - period=month → from 1st of this month to today
      - period=custom&start=YYYY-MM-DD&end=YYYY-MM-DD → arbitrary range
    """
    period = request.args.get("period", "week")
    if period == "custom":
        start_str = (request.args.get("start") or "").strip()
        end_str = (request.args.get("end") or "").strip()
        try:
            start = datetime.strptime(start_str, "%Y-%m-%d").date()
            end = datetime.strptime(end_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            abort(400, "start and end must be YYYY-MM-DD for period=custom")
        if start > end:
            abort(400, "start must be <= end")
        if (end - start).days + 1 > MAX_CUSTOM_RANGE_DAYS:
            abort(400, f"range too wide (max {MAX_CUSTOM_RANGE_DAYS} days)")
        return "custom", start, end
    if period not in ("week", "month"):
        period = "week"
    start, end = helpers.period_range(period)
    return period, start, end


@app.route("/api/aggregate")
def api_aggregate():
    period, start, end = _resolve_period()
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

    # Plans relevant to this period only.
    # For custom range we skip plans — they're calibrated for week/month.
    plans_out = {}
    if period in ("week", "month"):
        for (sp, key), p in db.get_plans().items():
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
