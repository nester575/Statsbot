"""Pure aggregation logic — separates numeric metrics from comments,
sums by metric, builds per-day series, computes averages.

Stays a pure function (no DB) for easy testing.
"""
from helpers import parse_number


def aggregate_reports(rows, text_keys=None):
    """Aggregate raw report rows into per-specialist summary structure.

    Args:
        rows: iterable of dicts {date, specialist, metric, value}
        text_keys: set of metric_keys to treat as text (go to comments,
                   not summed). If None, defaults to empty set — rows
                   with non-numeric values still fall back to comments.

    Returns:
        {specialist: {
            "metrics":  {metric_key: total},
            "averages": {metric_key: total/days_submitted},
            "comments": [{date, metric, value}, ...],
            "days_submitted": int,
            "series":   {metric_key: [{date, value}, ...]}  # sorted ASC
        }}
    """
    if text_keys is None:
        text_keys = set()
    result = {}

    for r in rows:
        sp = r["specialist"]
        bucket = result.setdefault(sp, {
            "metrics": {},
            "comments": [],
            "_days": set(),
            "_series": {},
        })
        bucket["_days"].add(r["date"])
        metric = r["metric"]
        value = (r["value"] or "").strip()
        num = None if metric in text_keys else parse_number(value)
        if num is not None:
            bucket["metrics"][metric] = bucket["metrics"].get(metric, 0) + num
            day_map = bucket["_series"].setdefault(metric, {})
            day_map[r["date"]] = day_map.get(r["date"], 0) + num
        else:
            if value and value != "-":
                bucket["comments"].append({
                    "date": r["date"],
                    "metric": metric,
                    "value": value,
                })

    def _round(v):
        return int(v) if float(v).is_integer() else round(v, 2)

    for sp, b in result.items():
        b["days_submitted"] = len(b["_days"])
        del b["_days"]
        days = b["days_submitted"] or 1
        b["averages"] = {k: _round(v / days) for k, v in b["metrics"].items()}
        b["metrics"] = {k: _round(v) for k, v in b["metrics"].items()}
        b["series"] = {
            metric: [{"date": d, "value": _round(v)} for d, v in sorted(daily.items())]
            for metric, daily in b["_series"].items()
        }
        del b["_series"]

    return result
