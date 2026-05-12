"""Entry point. Imports modules for side-effects (route registration, etc.)
and runs the bot + Flask app. Re-exports common symbols for backwards
compatibility with tests that import directly from `bot`.
"""
import logging
import threading

import config
import db
import tg_bot
import web_dashboard  # noqa: F401  -- registers dashboard routes on app
import web_admin      # noqa: F401  -- registers admin routes on app
from web_app import app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ============================================================
# Re-exports for backwards compatibility
# These let tests / external code do `bot.parse_number(...)` etc.
# ============================================================

# config
BOT_TOKEN     = config.BOT_TOKEN
DATABASE_URL  = config.DATABASE_URL
BOSS_ID       = config.BOSS_ID
ADMIN_TOKEN   = config.ADMIN_TOKEN
PORT          = config.PORT
BISHKEK       = config.BISHKEK
HOLIDAYS      = config.HOLIDAYS
ASKING        = config.ASKING
DEFAULT_QUESTIONS        = config.DEFAULT_QUESTIONS
DEFAULT_SPECIALIST_ORDER = config.DEFAULT_SPECIALIST_ORDER

# helpers
from helpers import parse_number, parse_hhmm, is_working_day, period_range  # noqa: F401,E402

# aggregate
from aggregate import aggregate_reports  # noqa: F401,E402

# db
from db import (  # noqa: F401,E402
    init_pool, init_db, get_conn,
    save_report, get_today_reports, get_period_reports,
    get_questions, get_config_lookups, get_plans, get_admin_config,
    get_specialists_dict, get_specialists_order,
    get_specialists_admin, get_admin_specialist_groups,
    get_setting, set_setting,
    get_report_for_day, upsert_report, delete_report_for_day,
)

# tg_bot — handlers and scheduling
from tg_bot import (  # noqa: F401,E402
    user_sessions, start, handle_answer, cancel,
    reminder_job, schedule_reminder, _do_schedule, _post_init,
    send_telegram_message,
    run_bot,
)

# Aliases the admin endpoint reads on each call (allows monkeypatching for tests)
def _get_TG_APP():
    return tg_bot.TG_APP
def _get_TG_LOOP():
    return tg_bot.TG_LOOP
TG_APP  = property(lambda self: tg_bot.TG_APP)   # not used; left as docs
TG_LOOP = property(lambda self: tg_bot.TG_LOOP)


def run_flask():
    app.run(host="0.0.0.0", port=PORT)


def main():
    init_pool()
    init_db()
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    run_bot()


if __name__ == "__main__":
    main()
