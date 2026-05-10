"""Telegram bot: handlers, scheduling, and message-send helpers."""
import asyncio
import logging
from datetime import datetime, time as dtime

from telegram import Update, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler,
)

import config
import db
from helpers import is_working_day, parse_hhmm

logger = logging.getLogger(__name__)

# In-memory survey state (lost on restart — acceptable trade-off)
user_sessions = {}

# Set in run_bot() — used by Flask thread to push live config changes
TG_APP = None
TG_LOOP = None


# ============================================================
# Conversation handlers
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    name = db.get_specialists_dict().get(user_id)
    if not name:
        await update.message.reply_text(
            f"❌ Ты не зарегистрирован.\nСообщи администратору свой ID: {user_id}"
        )
        return ConversationHandler.END
    questions = db.get_questions(name)
    if not questions:
        await update.message.reply_text(
            "⚠️ У тебя пока нет настроенных вопросов. Сообщи администратору."
        )
        return ConversationHandler.END
    user_sessions[user_id] = {"name": name, "questions": questions, "step": 0, "answers": {}}
    now_str = datetime.now(config.BISHKEK).strftime("%d.%m.%Y")
    await update.message.reply_text(
        f"👋 Привет, {name}!\n📅 Отчёт за {now_str}\nВсего {len(questions)} вопроса 👇",
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.message.reply_text(questions[0][1])
    return config.ASKING


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    session = user_sessions.get(user_id)
    if not session:
        await update.message.reply_text("Напиши /start чтобы начать.")
        return ConversationHandler.END

    questions = session["questions"]
    step = session["step"]
    session["answers"][questions[step][0]] = update.message.text.strip()
    session["step"] += 1

    if session["step"] < len(questions):
        progress = f"[{session['step']}/{len(questions)}] "
        await update.message.reply_text(progress + questions[session["step"]][1])
        return config.ASKING

    name = session["name"]
    answers = session["answers"]
    try:
        db.save_report(name, answers)
        status = "✅ Данные сохранены"
    except Exception as e:
        logger.error(f"DB error: {e}")
        status = "⚠️ Ошибка сохранения"

    lines = [f"✅ Отчёт принят, {name}!\n", status, ""]
    for k, v in answers.items():
        lines.append(f"  • {k}: {v}")
    await update.message.reply_text("\n".join(lines), reply_markup=ReplyKeyboardRemove())

    if config.BOSS_ID:
        boss_lines = [f"📊 *{name}* — {datetime.now(config.BISHKEK).strftime('%d.%m %H:%M')}\n"]
        for k, v in answers.items():
            boss_lines.append(f"  {k}: `{v}`")
        try:
            await context.bot.send_message(
                config.BOSS_ID, "\n".join(boss_lines), parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Boss error: {e}")

    del user_sessions[user_id]
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id in user_sessions:
        del user_sessions[user_id]
    await update.message.reply_text(
        "❌ Отменён. /start — начать заново.", reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(config.BISHKEK).date()
    if not is_working_day(today):
        logger.info(f"Skip reminder: {today} is non-working day")
        return
    specialists = db.get_specialists_dict()
    for user_id, name in specialists.items():
        try:
            await context.bot.send_message(
                chat_id=user_id, text=f"⏰ {name}, время отчёта!\nНапиши /start 👇"
            )
        except Exception as e:
            logger.error(f"Reminder error {name}: {e}")


# ============================================================
# Reminder scheduling (cross-thread safe)
# ============================================================

def _do_schedule(application, hh_mm):
    parsed = parse_hhmm(hh_mm) or (9, 0)
    h, m = parsed
    for job in application.job_queue.get_jobs_by_name("reminder_job"):
        job.schedule_removal()
    application.job_queue.run_daily(
        reminder_job,
        time=dtime(hour=h, minute=m, tzinfo=config.BISHKEK),
        days=(0, 1, 2, 3, 4),
        name="reminder_job",
    )
    return h, m


def schedule_reminder(application, hh_mm):
    """Reschedule the daily reminder. Safe to call from any thread.

    - Before the bot loop starts (initial setup): runs synchronously.
    - After loop is running (e.g., from Flask): hops over via
      asyncio.run_coroutine_threadsafe to the bot's event loop, since
      python-telegram-bot's AsyncIOScheduler is loop-bound.
    """
    if TG_LOOP is None or not TG_LOOP.is_running():
        h, m = _do_schedule(application, hh_mm)
        logger.info(f"Reminder scheduled (sync) at {h:02d}:{m:02d} Bishkek")
        return

    async def _coro():
        return _do_schedule(application, hh_mm)

    fut = asyncio.run_coroutine_threadsafe(_coro(), TG_LOOP)
    h, m = fut.result(timeout=5)
    logger.info(f"Reminder rescheduled (cross-thread) at {h:02d}:{m:02d} Bishkek")


async def _post_init(application):
    global TG_LOOP
    TG_LOOP = asyncio.get_running_loop()
    logger.info("Telegram event loop captured for cross-thread access")


# ============================================================
# Manual send (called from Flask /admin/api/send-reminder)
# ============================================================

def send_telegram_message(chat_id, text):
    """Synchronous one-shot — for manual reminders triggered from admin UI."""
    import httpx
    r = httpx.post(
        f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


# ============================================================
# Entry point
# ============================================================

def run_bot():
    global TG_APP
    application = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )
    TG_APP = application

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={config.ASKING: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_answer)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(conv)
    schedule_reminder(application, db.get_setting("reminder_time", "09:00"))
    logger.info("Бот запущен")
    application.run_polling()
