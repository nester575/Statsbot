"""Database layer: connection pool, schema migration, all SQL helpers.

The module exposes get_conn() as a context manager. With pool initialised
(call init_pool() at startup), connections are reused. Otherwise falls back
to per-request psycopg2.connect() — no behavioral difference.
"""
import logging
from contextlib import contextmanager
from datetime import datetime

import psycopg2
import psycopg2.extras
import psycopg2.pool

import config

logger = logging.getLogger(__name__)


# ============================================================
# Connection pool
# ============================================================

_db_pool = None


def init_pool():
    """Initialise the threaded connection pool. Call at app start."""
    global _db_pool
    try:
        _db_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=config.DB_POOL_MIN,
            maxconn=config.DB_POOL_MAX,
            dsn=config.DATABASE_URL,
            sslmode="require",
        )
        logger.info(
            f"DB pool initialized (min={config.DB_POOL_MIN}, max={config.DB_POOL_MAX})"
        )
    except Exception as e:
        logger.error(f"DB pool init failed; fallback to per-request connections: {e}")
        _db_pool = None


@contextmanager
def get_conn():
    """Yield a connection. Auto-commits on success, rolls back on exception.
    Uses the pool if available; otherwise opens a fresh connection.
    """
    if _db_pool is None:
        conn = psycopg2.connect(config.DATABASE_URL, sslmode="require")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return

    conn = _db_pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _db_pool.putconn(conn)


# ============================================================
# Schema + migration
# ============================================================

def init_db():
    """Create tables if missing; seed defaults on first run.
    Idempotent: safe to call on every startup.
    """
    import os

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reports (
                    id         SERIAL PRIMARY KEY,
                    date       DATE NOT NULL,
                    time       TIME NOT NULL,
                    specialist TEXT NOT NULL,
                    metric     TEXT NOT NULL,
                    value      TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS metrics_config (
                    id            SERIAL PRIMARY KEY,
                    specialist    TEXT NOT NULL,
                    metric_key    TEXT NOT NULL,
                    display_name  TEXT NOT NULL,
                    question_text TEXT NOT NULL,
                    position      INT NOT NULL DEFAULT 0,
                    is_text       BOOLEAN NOT NULL DEFAULT FALSE,
                    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
                    UNIQUE (specialist, metric_key)
                )
            """)
            cur.execute("ALTER TABLE metrics_config ADD COLUMN IF NOT EXISTS plan_week NUMERIC")
            cur.execute("ALTER TABLE metrics_config ADD COLUMN IF NOT EXISTS plan_month NUMERIC")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS specialists (
                    id          SERIAL PRIMARY KEY,
                    name        TEXT NOT NULL UNIQUE,
                    telegram_id TEXT NOT NULL,
                    position    INT NOT NULL DEFAULT 0,
                    is_active   BOOLEAN NOT NULL DEFAULT TRUE
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS report_edits (
                    id          SERIAL PRIMARY KEY,
                    edited_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    report_date DATE NOT NULL,
                    specialist  TEXT NOT NULL,
                    metric      TEXT NOT NULL,
                    old_value   TEXT,
                    new_value   TEXT,
                    edited_via  TEXT NOT NULL,
                    edited_by   TEXT
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS report_edits_edited_at_idx "
                "ON report_edits (edited_at DESC)"
            )

            # Seed metrics_config from DEFAULT_QUESTIONS (one-time)
            cur.execute("SELECT COUNT(*) FROM metrics_config")
            if cur.fetchone()[0] == 0:
                for specialist, items in config.DEFAULT_QUESTIONS.items():
                    for pos, (key, display, question, is_text) in enumerate(items):
                        cur.execute(
                            "INSERT INTO metrics_config "
                            "(specialist, metric_key, display_name, question_text, position, is_text) "
                            "VALUES (%s,%s,%s,%s,%s,%s)",
                            (specialist, key, display, question, pos, is_text),
                        )

            # Seed specialists from ID_* env vars (one-time legacy migration)
            cur.execute("SELECT COUNT(*) FROM specialists")
            if cur.fetchone()[0] == 0:
                env_map = {
                    "Эльдана":      os.environ.get("ID_ELDANA", ""),
                    "Станислав":    os.environ.get("ID_STANISLAV", ""),
                    "Мадина":       os.environ.get("ID_MADINA", ""),
                    "Олег":         os.environ.get("ID_OLEG", ""),
                    "Атай":         os.environ.get("ID_ATAY", ""),
                    "Производство": os.environ.get("ID_PRODUCTION", ""),
                }
                for pos, name in enumerate(config.DEFAULT_SPECIALIST_ORDER):
                    tg_id = env_map.get(name, "").strip()
                    if not tg_id:
                        continue
                    cur.execute(
                        "INSERT INTO specialists (name, telegram_id, position) "
                        "VALUES (%s,%s,%s) ON CONFLICT (name) DO NOTHING",
                        (name, tg_id, pos),
                    )

            cur.execute(
                "INSERT INTO settings (key, value) VALUES ('reminder_time', '09:00') "
                "ON CONFLICT (key) DO NOTHING"
            )


# ============================================================
# Reports
# ============================================================

def save_report(name, answers):
    """Insert one row per (metric, value) pair from a completed survey."""
    now = datetime.now(config.BISHKEK)
    with get_conn() as conn:
        with conn.cursor() as cur:
            for metric, value in answers.items():
                cur.execute(
                    "INSERT INTO reports (date, time, specialist, metric, value) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (now.date(), now.time(), name, metric, value),
                )


def get_today_reports():
    today = datetime.now(config.BISHKEK).date()
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, date::text, time::text, specialist, metric, value "
                "FROM reports WHERE date = %s ORDER BY time ASC",
                (today,),
            )
            return cur.fetchall()


def get_period_reports(start_date, end_date):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, date::text, time::text, specialist, metric, value "
                "FROM reports WHERE date >= %s AND date <= %s "
                "ORDER BY date ASC, time ASC",
                (start_date, end_date),
            )
            return cur.fetchall()


def get_report_for_day(specialist, date):
    """Returns {metric_key: value} for a given specialist+date.
    Empty dict if no entries.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT metric, value FROM reports "
                "WHERE specialist = %s AND date = %s",
                (specialist, date),
            )
            return {m: v for m, v in cur.fetchall()}


def upsert_report(specialist, date, values, time_str="18:00:00"):
    """Replace all rows for (specialist, date) with new values.
    Used for retroactive entry / corrections from admin UI.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM reports WHERE specialist = %s AND date = %s",
                (specialist, date),
            )
            for metric, value in values.items():
                cur.execute(
                    "INSERT INTO reports (date, time, specialist, metric, value) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (date, time_str, specialist, metric, str(value)),
                )


def upsert_report_with_audit(specialist, date, new_values, via, by=None, time_str=None):
    """Replace rows for (specialist, date) and log per-metric diffs.

    - Preserves the original `time` of the report if one exists, so a retroactive
      edit doesn't move the entry to a new submission time.
    - Falls back to `time_str` (or 18:00 default) when no prior entry exists.
    - Writes one row to `report_edits` per ACTUAL change (added / changed / removed).
      Unchanged metrics produce no audit noise.

    Args:
        specialist: name
        date: datetime.date
        new_values: {metric_key: value} — full desired state for the day
        via: 'bot' (self-edit) or 'admin' (retro from web UI)
        by: telegram_id of editor (only meaningful when via='bot')
        time_str: override; if None, original time is preserved

    Returns:
        (changes_count, time_used_str)
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT metric, value, time::text FROM reports "
                "WHERE specialist = %s AND date = %s",
                (specialist, date),
            )
            existing = cur.fetchall()
            old_map = {m: v for m, v, _ in existing}

            if time_str is None:
                time_str = existing[0][2] if existing else "18:00:00"

            cur.execute(
                "DELETE FROM reports WHERE specialist = %s AND date = %s",
                (specialist, date),
            )
            for metric, value in new_values.items():
                cur.execute(
                    "INSERT INTO reports (date, time, specialist, metric, value) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (date, time_str, specialist, metric, str(value)),
                )

            new_map = {k: str(v) for k, v in new_values.items()}
            all_metrics = set(old_map) | set(new_map)
            changes = 0
            for metric in sorted(all_metrics):
                old_v = old_map.get(metric)
                new_v = new_map.get(metric)
                if old_v != new_v:
                    cur.execute(
                        "INSERT INTO report_edits "
                        "(report_date, specialist, metric, old_value, new_value, "
                        "edited_via, edited_by) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (date, specialist, metric, old_v, new_v, via, by),
                    )
                    changes += 1
            return changes, time_str


def get_recent_edits(limit=200):
    """Audit log entries, newest first. Used by admin /admin/api/edits."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, edited_at::text AS edited_at, report_date::text AS report_date, "
                "specialist, metric, old_value, new_value, edited_via, edited_by "
                "FROM report_edits "
                "ORDER BY edited_at DESC, id DESC LIMIT %s",
                (int(limit),),
            )
            return [dict(r) for r in cur.fetchall()]


def delete_report_for_day(specialist, date, via="admin", by=None):
    """Delete all rows for (specialist, date). Logs each removed metric to audit.
    Returns count deleted.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT metric, value FROM reports "
                "WHERE specialist = %s AND date = %s",
                (specialist, date),
            )
            existing = cur.fetchall()
            cur.execute(
                "DELETE FROM reports WHERE specialist = %s AND date = %s",
                (specialist, date),
            )
            n = cur.rowcount
            for metric, value in existing:
                cur.execute(
                    "INSERT INTO report_edits "
                    "(report_date, specialist, metric, old_value, new_value, "
                    "edited_via, edited_by) "
                    "VALUES (%s, %s, %s, %s, NULL, %s, %s)",
                    (date, specialist, metric, value, via, by),
                )
            return n


# ============================================================
# Metrics config
# ============================================================

def get_questions(specialist):
    """List of (metric_key, question_text) tuples — used by /start."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT metric_key, question_text FROM metrics_config "
                "WHERE specialist = %s AND is_active = TRUE "
                "ORDER BY position ASC, id ASC",
                (specialist,),
            )
            return [(k, q) for k, q in cur.fetchall()]


def get_config_lookups():
    """Returns (display_names, text_keys) used by dashboard rendering."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT metric_key, display_name, is_text, is_active FROM metrics_config"
            )
            display = {}
            text_keys = set()
            for k, d, is_text, _is_active in cur.fetchall():
                display[k] = d
                if is_text:
                    text_keys.add(k)
            return display, text_keys


def get_plans():
    """Returns {(specialist, metric_key): {'week': float|None, 'month': float|None}}."""
    plans = {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT specialist, metric_key, plan_week, plan_month FROM metrics_config"
            )
            for sp, k, pw, pm in cur.fetchall():
                plans[(sp, k)] = {
                    "week":  float(pw) if pw is not None else None,
                    "month": float(pm) if pm is not None else None,
                }
    return plans


def get_admin_config():
    """Full metrics_config rows for admin UI (active and inactive)."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, specialist, metric_key, display_name, question_text, "
                "position, is_text, is_active, plan_week, plan_month FROM metrics_config "
                "ORDER BY specialist, position ASC, id ASC"
            )
            rows = []
            for r in cur.fetchall():
                d = dict(r)
                d["plan_week"]  = float(d["plan_week"])  if d["plan_week"]  is not None else None
                d["plan_month"] = float(d["plan_month"]) if d["plan_month"] is not None else None
                rows.append(d)
            return rows


def get_active_metrics_for(specialist):
    """Active metric_config rows for one specialist, ordered for UI rendering.

    Used by the retroactive report form to know which fields to show.
    SQL-side filter avoids fetching all rows just to drop most of them.
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, specialist, metric_key, display_name, question_text, "
                "position, is_text, is_active, plan_week, plan_month FROM metrics_config "
                "WHERE specialist = %s AND is_active = TRUE "
                "ORDER BY position ASC, id ASC",
                (specialist,),
            )
            rows = []
            for r in cur.fetchall():
                d = dict(r)
                d["plan_week"]  = float(d["plan_week"])  if d["plan_week"]  is not None else None
                d["plan_month"] = float(d["plan_month"]) if d["plan_month"] is not None else None
                rows.append(d)
            return rows


# ============================================================
# Specialists
# ============================================================

def get_specialists_dict():
    """{telegram_id: name} for active specialists — used for auth in /start."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT telegram_id, name FROM specialists WHERE is_active = TRUE"
            )
            return {tg: name for tg, name in cur.fetchall() if tg}


def get_specialists_order():
    """Active specialists in display order — for dashboard column ordering."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name FROM specialists WHERE is_active = TRUE "
                "ORDER BY position ASC, id ASC"
            )
            return [r[0] for r in cur.fetchall()]


def get_specialists_admin():
    """All specialists (active + inactive) for admin specialists section."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, name, telegram_id, position, is_active FROM specialists "
                "ORDER BY position ASC, id ASC"
            )
            return [dict(r) for r in cur.fetchall()]


def get_admin_specialist_groups():
    """Names that should appear as cards in the admin metrics view.

    Includes inactive specialists AND any 'orphan' names found only in
    metrics_config (so admin can still see/edit those metrics).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name, is_active FROM specialists "
                "ORDER BY position ASC, id ASC"
            )
            from_table = [(n, a) for n, a in cur.fetchall()]
            in_table = {n for n, _ in from_table}
            cur.execute("SELECT DISTINCT specialist FROM metrics_config")
            orphans = sorted({r[0] for r in cur.fetchall()} - in_table)
    return [{"name": n, "is_active": a} for n, a in from_table] + \
           [{"name": n, "is_active": False, "orphan": True} for n in orphans]


# ============================================================
# Settings (k/v)
# ============================================================

def get_setting(key, default=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row else default


def set_setting(key, value):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO settings (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, str(value)),
            )
