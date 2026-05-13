"""Telegram bot: handlers, scheduling, and message-send helpers."""
import asyncio
import logging
from datetime import datetime, time as dtime, timedelta

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardRemove, Update,
)
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)

import config
import db
from helpers import is_working_day, parse_hhmm

logger = logging.getLogger(__name__)

# In-memory survey state (lost on restart — acceptable trade-off)
user_sessions = {}
# Separate dict for /edit so /start and /edit don't clobber each other
edit_sessions = {}

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


# ============================================================
# /edit conversation: self-service correction of today/yesterday
# ============================================================

# Magic value users type to leave a metric untouched during /edit
KEEP_MARKER = "="


async def edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for /edit — show inline buttons for today/yesterday."""
    user_id = str(update.effective_user.id)
    name = db.get_specialists_dict().get(user_id)
    if not name:
        await update.message.reply_text(
            f"❌ Ты не зарегистрирован.\nСообщи администратору свой ID: {user_id}"
        )
        return ConversationHandler.END

    today = datetime.now(config.BISHKEK).date()
    yesterday = today - timedelta(days=1)
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"📅 Сегодня ({today.strftime('%d.%m')})",
                callback_data=f"edit_date:{today.isoformat()}",
            ),
            InlineKeyboardButton(
                f"📅 Вчера ({yesterday.strftime('%d.%m')})",
                callback_data=f"edit_date:{yesterday.isoformat()}",
            ),
        ],
        [InlineKeyboardButton("❌ Отмена", callback_data="edit_cancel")],
    ])
    await update.message.reply_text(
        f"✏️ {name}, какой день правим?",
        reply_markup=keyboard,
    )
    return config.EDIT_PICK_DATE


async def edit_picked_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User tapped one of the date buttons. Load current values, start survey."""
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)

    if query.data == "edit_cancel":
        await query.edit_message_text("❌ Редактирование отменено.")
        return ConversationHandler.END

    try:
        _, date_iso = query.data.split(":", 1)
        target_date = datetime.strptime(date_iso, "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        await query.edit_message_text("⚠️ Некорректная дата. /edit чтобы попробовать снова.")
        return ConversationHandler.END

    name = db.get_specialists_dict().get(user_id)
    if not name:
        await query.edit_message_text("❌ Ты не зарегистрирован.")
        return ConversationHandler.END

    existing = db.get_report_for_day(name, target_date)
    if not existing:
        date_label = target_date.strftime("%d.%m.%Y")
        await query.edit_message_text(
            f"❌ За {date_label} отчёта нет — нечего править.\n"
            "Если забыл сдать — попроси админа сделать ретро-ввод."
        )
        return ConversationHandler.END

    metrics = db.get_active_metrics_for(name)
    if not metrics:
        await query.edit_message_text("⚠️ У тебя нет активных метрик.")
        return ConversationHandler.END

    # Build prompts: every active metric, with current value (or "—" if absent)
    questions = []
    for m in metrics:
        key = m["metric_key"]
        cur_val = existing.get(key, "—")
        prompt = (
            f"{m['question_text']}\n"
            f"📌 Сейчас: {cur_val}\n"
            f'Введи новое значение или «{KEEP_MARKER}» чтобы оставить как есть.'
        )
        questions.append((key, prompt))

    edit_sessions[user_id] = {
        "name": name,
        "date": target_date,
        "questions": questions,
        "existing": dict(existing),
        "step": 0,
        "answers": {},
    }

    date_label = target_date.strftime("%d.%m.%Y")
    await query.edit_message_text(
        f"✏️ Правим отчёт {name} за {date_label}.\n"
        f"Всего вопросов: {len(questions)}.\n"
        f"💡 «{KEEP_MARKER}» — оставить как есть. /cancel — отменить."
    )
    await context.bot.send_message(query.message.chat_id, questions[0][1])
    return config.EDIT_ASKING


async def edit_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle each answer in /edit. '=' keeps existing value."""
    user_id = str(update.effective_user.id)
    session = edit_sessions.get(user_id)
    if not session:
        await update.message.reply_text(
            "Сессия редактирования потеряна. /edit чтобы начать заново."
        )
        return ConversationHandler.END

    questions = session["questions"]
    step = session["step"]
    answer = update.message.text.strip()
    metric_key = questions[step][0]

    if answer == KEEP_MARKER:
        # Preserve existing value; if metric wasn't set before, leave unset
        if metric_key in session["existing"]:
            session["answers"][metric_key] = session["existing"][metric_key]
    else:
        session["answers"][metric_key] = answer

    session["step"] += 1
    if session["step"] < len(questions):
        progress = f"[{session['step']}/{len(questions)}] "
        await update.message.reply_text(progress + questions[session["step"]][1])
        return config.EDIT_ASKING

    # Finalize: write through audit-aware upsert
    name = session["name"]
    target_date = session["date"]
    answers = session["answers"]

    if not answers:
        await update.message.reply_text(
            "⚠️ Все ответы пустые — ничего не сохранено.",
            reply_markup=ReplyKeyboardRemove(),
        )
        del edit_sessions[user_id]
        return ConversationHandler.END

    try:
        changes, _time = db.upsert_report_with_audit(
            name, target_date, answers,
            via="bot", by=user_id,
        )
        if changes == 0:
            status = "ℹ️ Изменений не обнаружено"
        else:
            status = f"✅ Сохранено ({changes} изменений в журнале)"
    except Exception as e:
        logger.error(f"Edit DB error: {e}")
        status = "⚠️ Ошибка сохранения"

    date_label = target_date.strftime("%d.%m.%Y")
    lines = [f"✏️ Отчёт {name} за {date_label} обновлён:\n", status, ""]
    for k, v in answers.items():
        lines.append(f"  • {k}: {v}")
    await update.message.reply_text(
        "\n".join(lines), reply_markup=ReplyKeyboardRemove()
    )

    del edit_sessions[user_id]
    return ConversationHandler.END


async def edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel /edit at any stage."""
    user_id = str(update.effective_user.id)
    if user_id in edit_sessions:
        del edit_sessions[user_id]
    await update.message.reply_text(
        "❌ Правка отменена.", reply_markup=ReplyKeyboardRemove()
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

    edit_conv = ConversationHandler(
        entry_points=[CommandHandler("edit", edit_start)],
        states={
            config.EDIT_PICK_DATE: [
                CallbackQueryHandler(edit_picked_date, pattern=r"^edit_(date|cancel)")
            ],
            config.EDIT_ASKING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_answer)
            ],
        },
        fallbacks=[CommandHandler("cancel", edit_cancel)],
    )
    application.add_handler(edit_conv)
    schedule_reminder(application, db.get_setting("reminder_time", "09:00"))
    logger.info("Бот запущен")
    application.run_polling()
