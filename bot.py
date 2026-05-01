import os
import logging
import threading
from datetime import datetime
from flask import Flask, render_template_string, jsonify
import psycopg2
import psycopg2.extras
import pytz
from telegram import Update, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

BOT_TOKEN    = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
BOSS_ID      = os.environ.get("BOSS_ID", "")
PORT         = int(os.environ.get("PORT", 8080))
BISHKEK      = pytz.timezone("Asia/Bishkek")

SPECIALISTS = {
    os.environ.get("ID_ELDANA",      ""): "Эльдана",
    os.environ.get("ID_STANISLAV",   ""): "Станислав",
    os.environ.get("ID_MADINA",      ""): "Мадина",
    os.environ.get("ID_OLEG",        ""): "Олег",
    os.environ.get("ID_ATAY",        ""): "Атай",
    os.environ.get("ID_PRODUCTION",  ""): "Производство",
}
QUESTIONS = {
    "Эльдана": [
        ("заявки",    "📥 Сколько заявок получила сегодня?"),
        ("письма",    "📧 Сколько исходящих писем отправила?"),
        ("рассылки",  "📨 Сколько рассылок сделала?"),
        ("комментарий", "💬 Комментарий (или «-»)"),
    ],
    "Станислав": [
        ("контакты",       "📞 Сколько исходящих контактов?"),
        ("кп",             "📄 Сколько КП отправил?"),
        ("договора",       "✍️ Сколько договоров заключил?"),
        ("объекты_работа", "🏗 Объектов в работе?"),
        ("объекты_разраб", "🔍 Объектов в разработке?"),
        ("доход",          "💰 Валовый доход за день (0 если нет)?"),
        ("комментарий",    "💬 Комментарий (или «-»)"),
    ],
    "Мадина": [
        ("контакты",       "📞 Сколько исходящих контактов?"),
        ("пакеты",         "📦 Сколько пакетов продала?"),
        ("договора",       "✍️ Сколько договоров заключила?"),
        ("объекты_работа", "🏗 Объектов в работе?"),
        ("комментарий",    "💬 Комментарий (или «-»)"),
    ],
    "Олег": [
        ("контакты",       "📞 Сколько контактов сегодня?"),
        ("кп",             "📄 Сколько КП отправил?"),
        ("клиенты_работа", "🏛 Клиентов в работе?"),
        ("комментарий",    "💬 Комментарий (или «-»)"),
    ],
    "Атай": [
        ("тендеры_найдено",  "🔎 Сколько тендеров нашёл?"),
        ("заявки_подготовл", "📝 Сколько заявок подготовил?"),
        ("заявки_подано",    "📬 Сколько заявок подал?"),
        ("заявки_отклонено", "❌ Сколько заявок отклонено?"),
        ("тендеры_выиграно", "🏆 Сколько тендеров выиграл?"),
        ("сумма_подано",     "💰 Сумма поданных предложений?"),
        ("комментарий",      "💬 Комментарий (или «-»)"),
    ],
    "Производство": [
        ("сделано",     "✅ Что сделали сегодня?"),
        ("план_завтра", "📋 План на завтра?"),
        ("проблемы",    "⚠️ Проблемы / риски (или «-»)"),
    ],
}

ASKING = 1
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
user_sessions = {}
def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
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
        conn.commit()

def save_report(name, answers):
    now = datetime.now(BISHKEK)
    with get_conn() as conn:
        with conn.cursor() as cur:
            for metric, value in answers.items():
                cur.execute(
                    "INSERT INTO reports (date, time, specialist, metric, value) VALUES (%s, %s, %s, %s, %s)",
                    (now.date(), now.time(), name, metric, value)
                )
        conn.commit()

def get_today_reports():
    today = datetime.now(BISHKEK).date()
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, date::text, time::text, specialist, metric, value FROM reports WHERE date = %s ORDER BY time ASC", (today,))
            return cur.fetchall()

def get_week_reports():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, date::text, time::text, specialist, metric, value FROM reports WHERE date >= CURRENT_DATE - INTERVAL '7 days' ORDER BY date DESC, time ASC")
            return cur.fetchall()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    name = SPECIALISTS.get(user_id)
    if not name:
        await update.message.reply_text(f"❌ Ты не зарегистрирован.\nСообщи администратору свой ID: {user_id}")
        return ConversationHandler.END
    user_sessions[user_id] = {"name": name, "questions": QUESTIONS[name], "step": 0, "answers": {}}
    now_str = datetime.now(BISHKEK).strftime("%d.%m.%Y")
    await update.message.reply_text(f"👋 Привет, {name}!\n📅 Отчёт за {now_str}\nВсего {len(QUESTIONS[name])} вопроса 👇", reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text(QUESTIONS[name][0][1])
    return ASKING

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
        return ASKING
    name = session["name"]
    answers = session["answers"]
    try:
        save_report(name, answers)
        status = "✅ Данные сохранены"
    except Exception as e:
        logger.error(f"DB error: {e}")
        status = "⚠️ Ошибка сохранения"
    lines = [f"✅ Отчёт принят, {name}!\n", status, ""]
    for k, v in answers.items():
        lines.append(f"  • {k}: {v}")
    await update.message.reply_text("\n".join(lines), reply_markup=ReplyKeyboardRemove())
    if BOSS_ID:
        boss_lines = [f"📊 *{name}* — {datetime.now(BISHKEK).strftime('%d.%m %H:%M')}\n"]
        for k, v in answers.items():
            boss_lines.append(f"  {k}: `{v}`")
        try:
            await context.bot.send_message(BOSS_ID, "\n".join(boss_lines), parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Boss error: {e}")
    del user_sessions[user_id]
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id in user_sessions:
        del user_sessions[user_id]
    await update.message.reply_text("❌ Отменён. /start — начать заново.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    for user_id, name in SPECIALISTS.items():
        if not user_id:
            continue
        try:
            await context.bot.send_message(chat_id=user_id, text=f"⏰ {name}, время отчёта!\nНапиши /start 👇")
        except Exception as e:
            logger.error(f"Reminder error {name}: {e}")
app = Flask(__name__)

@app.route("/")
def dashboard():
    return render_template_string("""<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="120"><title>Дашборд</title><style>body{background:#0a0c10;color:#e2e6f0;font-family:sans-serif;padding:24px}h1{color:#e8ff47;font-size:36px;margin-bottom:8px}.live{color:#4ade80;font-size:12px;margin-bottom:24px}.card{background:#11141b;border:1px solid #232838;padding:20px;margin-bottom:16px}.name{color:#47c8ff;font-size:18px;font-weight:bold;margin-bottom:12px}table{width:100%;border-collapse:collapse}td,th{padding:8px 12px;border-bottom:1px solid #232838;font-size:13px}th{color:#5a6070;font-size:11px;text-transform:uppercase}.chip{display:inline-block;padding:4px 12px;border-radius:2px;font-size:11px;margin:4px}.done{background:rgba(74,222,128,.15);color:#4ade80;border:1px solid rgba(74,222,128,.3)}.pending{background:rgba(90,96,112,.1);color:#5a6070;border:1px solid #232838}.num{color:#e8ff47;font-size:20px;font-weight:bold}</style></head><body><h1>LIVE ДАШБОРД</h1><div class="live">● Обновляется каждые 2 минуты</div><div id="status"></div><div id="data">Загрузка...</div><script>const SP=['Эльдана','Станислав','Мадина','Олег','Атай','Производство'];async function load(){const[t,w]=await Promise.all([fetch('/api/today').then(r=>r.json()),fetch('/api/week').then(r=>r.json())]);const sub=new Set(t.map(r=>r.specialist));document.getElementById('status').innerHTML='<div class="card"><div class="name">Статус сдачи сегодня</div>'+SP.map(n=>`<span class="chip ${sub.has(n)?'done':'pending'}">${sub.has(n)?'✓':''} ${n}</span>`).join('')+'</div>';const bp={};t.forEach(r=>{if(!bp[r.specialist])bp[r.specialist]=[];bp[r.specialist].push(r)});let h='';for(const[n,rows]of Object.entries(bp)){h+=`<div class="card"><div class="name">${n}</div><table>`;rows.forEach(r=>{const isN=!isNaN(r.value)&&!['комментарий','сделано','план_завтра','проблемы'].includes(r.metric);h+=`<tr><td style="color:#5a6070">${r.metric}</td><td>${isN?`<span class="num">${r.value}</span>`:r.value}</td></tr>`});h+='</table></div>'}document.getElementById('data').innerHTML=h||'<div class="card">Отчётов за сегодня пока нет</div>'}load();setInterval(load,120000);</script></body></html>""")

@app.route("/api/today")
def api_today():
    rows = get_today_reports()
    return jsonify([dict(r) for r in rows])

@app.route("/api/week")
def api_week():
    rows = get_week_reports()
    return jsonify([dict(r) for r in rows])
    for r in rows:
        row = dict(r)
        row["date"] = str(row["date"])
        row["time"] = str(row["time"])
        result.append(row)
    return jsonify(result)

@app.route("/health")
def health():
    return "ok"

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

def run_bot():
    application = Application.builder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={ASKING: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_answer)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(conv)
    application.job_queue.run_daily(
        reminder_job,
        time=datetime.strptime("09:00", "%H:%M").time().replace(tzinfo=BISHKEK),
        days=(0, 1, 2, 3, 4),
    )
    init_db()
    logger.info("Бот запущен")
    application.run_polling()

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    run_bot()
