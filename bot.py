import os
import hmac
import logging
import threading
from datetime import datetime, time as dtime, date as ddate, timedelta
from flask import Flask, render_template_string, jsonify, request, abort
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
ADMIN_TOKEN  = os.environ.get("ADMIN_TOKEN", "")
PORT         = int(os.environ.get("PORT", 8080))
BISHKEK      = pytz.timezone("Asia/Bishkek")

HOLIDAYS = set()

def is_working_day(d):
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS

SPECIALISTS = {
    uid: name for uid, name in {
        os.environ.get("ID_ELDANA",      ""): "Эльдана",
        os.environ.get("ID_STANISLAV",   ""): "Станислав",
        os.environ.get("ID_MADINA",      ""): "Мадина",
        os.environ.get("ID_OLEG",        ""): "Олег",
        os.environ.get("ID_ATAY",        ""): "Атай",
        os.environ.get("ID_PRODUCTION",  ""): "Производство",
    }.items() if uid
}
SPECIALIST_ORDER = ["Эльдана", "Станислав", "Мадина", "Олег", "Атай", "Производство"]

DEFAULT_QUESTIONS = {
    "Эльдана": [
        ("заявки",       "заявки",       "📥 Сколько заявок получила сегодня?", False),
        ("письма",       "письма",       "📧 Сколько исходящих писем отправила?", False),
        ("рассылки",     "рассылки",     "📨 Сколько рассылок сделала?", False),
        ("комментарий",  "комментарий",  "💬 Комментарий (или «-»)", True),
    ],
    "Станислав": [
        ("контакты",       "контакты",       "📞 Сколько исходящих контактов?", False),
        ("кп",             "КП",             "📄 Сколько КП отправил?", False),
        ("договора",       "договора",       "✍️ Сколько договоров заключил?", False),
        ("объекты_работа", "объекты в работе", "🏗 Объектов в работе?", False),
        ("объекты_разраб", "объекты в разработке", "🔍 Объектов в разработке?", False),
        ("доход",          "доход",          "💰 Валовый доход за день (0 если нет)?", False),
        ("комментарий",    "комментарий",    "💬 Комментарий (или «-»)", True),
    ],
    "Мадина": [
        ("контакты",       "контакты",       "📞 Сколько исходящих контактов?", False),
        ("пакеты",         "пакеты",         "📦 Сколько пакетов продала?", False),
        ("договора",       "договора",       "✍️ Сколько договоров заключила?", False),
        ("объекты_работа", "объекты в работе", "🏗 Объектов в работе?", False),
        ("комментарий",    "комментарий",    "💬 Комментарий (или «-»)", True),
    ],
    "Олег": [
        ("контакты",       "контакты",       "📞 Сколько контактов сегодня?", False),
        ("кп",             "КП",             "📄 Сколько КП отправил?", False),
        ("клиенты_работа", "клиенты в работе", "🏛 Клиентов в работе?", False),
        ("комментарий",    "комментарий",    "💬 Комментарий (или «-»)", True),
    ],
    "Атай": [
        ("тендеры_найдено",  "тендеры найдено",  "🔎 Сколько тендеров нашёл?", False),
        ("заявки_подготовл", "заявки подготовл.", "📝 Сколько заявок подготовил?", False),
        ("заявки_подано",    "заявки подано",    "📬 Сколько заявок подал?", False),
        ("заявки_отклонено", "заявки отклонено", "❌ Сколько заявок отклонено?", False),
        ("тендеры_выиграно", "тендеры выиграно", "🏆 Сколько тендеров выиграл?", False),
        ("сумма_подано",     "сумма подано",     "💰 Сумма поданных предложений?", False),
        ("комментарий",      "комментарий",      "💬 Комментарий (или «-»)", True),
    ],
    "Производство": [
        ("сделано",     "сделано",     "✅ Что сделали сегодня?", True),
        ("план_завтра", "план на завтра", "📋 План на завтра?", True),
        ("проблемы",    "проблемы",    "⚠️ Проблемы / риски (или «-»)", True),
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
            cur.execute("SELECT COUNT(*) FROM metrics_config")
            if cur.fetchone()[0] == 0:
                for specialist, items in DEFAULT_QUESTIONS.items():
                    for pos, (key, display, question, is_text) in enumerate(items):
                        cur.execute(
                            "INSERT INTO metrics_config "
                            "(specialist, metric_key, display_name, question_text, position, is_text) "
                            "VALUES (%s,%s,%s,%s,%s,%s)",
                            (specialist, key, display, question, pos, is_text),
                        )
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

def get_questions(specialist):
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
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT metric_key, display_name, is_text, is_active FROM metrics_config")
            display = {}
            text_keys = set()
            for k, d, is_text, is_active in cur.fetchall():
                display[k] = d
                if is_text:
                    text_keys.add(k)
            return display, text_keys

def get_admin_config():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, specialist, metric_key, display_name, question_text, "
                "position, is_text, is_active FROM metrics_config "
                "ORDER BY specialist, position ASC, id ASC"
            )
            return cur.fetchall()

def parse_number(s):
    if s is None:
        return None
    cleaned = str(s).replace(" ", "").replace(" ", "").replace(",", ".").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None

def aggregate_reports(rows, text_keys=None):
    if text_keys is None:
        text_keys = set()
    result = {}
    for r in rows:
        sp = r["specialist"]
        bucket = result.setdefault(sp, {"metrics": {}, "comments": [], "_days": set(), "_series": {}})
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

def period_range(period):
    today = datetime.now(BISHKEK).date()
    if period == "month":
        start = today.replace(day=1)
    else:
        start = today - timedelta(days=6)
    return start, today

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    name = SPECIALISTS.get(user_id)
    if not name:
        await update.message.reply_text(f"❌ Ты не зарегистрирован.\nСообщи администратору свой ID: {user_id}")
        return ConversationHandler.END
    questions = get_questions(name)
    if not questions:
        await update.message.reply_text("⚠️ У тебя пока нет настроенных вопросов. Сообщи администратору.")
        return ConversationHandler.END
    user_sessions[user_id] = {"name": name, "questions": questions, "step": 0, "answers": {}}
    now_str = datetime.now(BISHKEK).strftime("%d.%m.%Y")
    await update.message.reply_text(f"👋 Привет, {name}!\n📅 Отчёт за {now_str}\nВсего {len(questions)} вопроса 👇", reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text(questions[0][1])
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
    today = datetime.now(BISHKEK).date()
    if not is_working_day(today):
        logger.info(f"Skip reminder: {today} is non-working day")
        return
    for user_id, name in SPECIALISTS.items():
        try:
            await context.bot.send_message(chat_id=user_id, text=f"⏰ {name}, время отчёта!\nНапиши /start 👇")
        except Exception as e:
            logger.error(f"Reminder error {name}: {e}")
app = Flask(__name__)

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="120">
<title>Live Dashboard ОсОО «Каравелла»</title>
<style>
body{background:#0a0c10;color:#e2e6f0;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;padding:24px;margin:0}
h1{color:#e8ff47;font-size:32px;margin:0 0 8px}
.live{color:#4ade80;font-size:12px;margin-bottom:20px}
.tabs{display:flex;gap:4px;margin-bottom:20px;border-bottom:1px solid #232838}
.tab{background:none;border:none;color:#5a6070;padding:12px 20px;font-size:14px;cursor:pointer;border-bottom:2px solid transparent;font-family:inherit}
.tab:hover{color:#e2e6f0}
.tab.active{color:#e8ff47;border-bottom-color:#e8ff47}
.period{color:#5a6070;font-size:12px;margin-bottom:16px}
.card{background:#11141b;border:1px solid #232838;padding:20px;margin-bottom:16px;border-radius:4px}
.name{color:#47c8ff;font-size:18px;font-weight:bold;margin-bottom:12px;display:flex;justify-content:space-between;align-items:center}
.days{color:#5a6070;font-size:11px;font-weight:normal}
table{width:100%;border-collapse:collapse}
td,th{padding:8px 12px;border-bottom:1px solid #232838;font-size:13px;text-align:left}
th{color:#5a6070;font-size:11px;text-transform:uppercase}
tr:last-child td{border-bottom:none}
.chip{display:inline-block;padding:4px 12px;border-radius:2px;font-size:11px;margin:4px}
.done{background:rgba(74,222,128,.15);color:#4ade80;border:1px solid rgba(74,222,128,.3)}
.pending{background:rgba(90,96,112,.1);color:#5a6070;border:1px solid #232838}
.num{color:#e8ff47;font-size:20px;font-weight:bold}
.empty{color:#5a6070;text-align:center;padding:24px}
details{margin-top:12px;border-top:1px solid #232838;padding-top:12px}
summary{color:#5a6070;font-size:12px;cursor:pointer;padding:4px 0}
summary:hover{color:#e2e6f0}
.cmt{padding:6px 0;font-size:13px;border-top:1px solid #1a1d26}
.cmt:first-child{border-top:none}
.cmt-date{color:#5a6070;font-size:11px;margin-right:8px}
.cmt-metric{color:#47c8ff;font-size:11px;margin-right:8px}
.hidden{display:none}
.today-row{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid #232838;gap:12px}
.today-row:last-child{border-bottom:none}
.today-row .label{color:#5a6070;font-size:13px;flex:1;min-width:0}
.today-row .value{flex:0 0 140px;text-align:right;color:#e2e6f0;font-size:14px;word-break:break-word}
.today-row .value .num{color:#e8ff47;font-size:20px;font-weight:bold}
.metric-row{display:grid;grid-template-columns:120px minmax(0,1fr) auto 130px;gap:14px;align-items:center;padding:14px 0;border-top:1px solid #1a1d26}
.metric-row:first-of-type{border-top:none}
.metric-row .label{color:#9097a8;font-size:13px}
.metric-row .days{display:flex;gap:4px;overflow-x:auto;padding-bottom:6px;scrollbar-width:thin;scrollbar-color:#2d3346 transparent}
.metric-row .days::-webkit-scrollbar{height:5px}
.metric-row .days::-webkit-scrollbar-thumb{background:#2d3346;border-radius:2px}
.day-cell{flex:0 0 auto;min-width:42px;text-align:center;padding:5px 6px;border-radius:5px;background:#0d1017;border:1px solid #1a1d26}
.day-cell .d{font-size:9px;color:#5a6070;line-height:1.1;letter-spacing:.3px}
.day-cell .v{font-size:13px;color:#e2e6f0;font-weight:600;line-height:1.3;margin-top:3px}
.day-cell.empty{background:transparent;border-style:dashed;border-color:#1a1d26}
.day-cell.empty .v{color:#2d3346;font-weight:400}
.metric-row .stats{display:flex;align-items:center;gap:10px;justify-content:flex-end}
.metric-row .stats .avg{color:#5a6070;font-size:11px;white-space:nowrap}
.metric-row .stats .total{color:#e8ff47;font-size:22px;font-weight:bold;min-width:48px;text-align:right}
.trend{display:inline-flex;align-items:center;gap:3px;font-size:11px;padding:2px 7px;border-radius:10px;font-weight:600;white-space:nowrap}
.trend.up{color:#4ade80;background:rgba(74,222,128,.12);border:1px solid rgba(74,222,128,.25)}
.trend.down{color:#ef6464;background:rgba(239,100,100,.1);border:1px solid rgba(239,100,100,.25)}
.trend.flat{color:#5a6070;background:rgba(90,96,112,.1);border:1px solid #232838}
.trend.new{color:#47c8ff;background:rgba(71,200,255,.1);border:1px solid rgba(71,200,255,.25)}
.chart-mini{background:#0d1017;border:1px solid #1a1d26;border-radius:6px;height:54px;padding:4px;transition:transform .18s ease,border-color .18s ease,box-shadow .18s ease;position:relative}
.chart-mini:hover{transform:scale(1.08);border-color:#2d3346;box-shadow:0 6px 20px rgba(0,0,0,.5);z-index:1}
.chart-mini svg{width:100%;height:100%;display:block;overflow:visible}
.chart-mini .dot{fill:#e8ff47;stroke:#0d1017;stroke-width:1.5;cursor:pointer;transition:r .15s ease}
.chart-mini .dot:hover{r:4}
.chart-mini .line{fill:none;stroke:#e8ff47;stroke-width:1.5;stroke-linejoin:round;stroke-linecap:round}
.chart-mini .area{opacity:.85}
.chart-empty{color:#2d3346;font-size:10px;text-align:center;padding-top:16px}
@media (max-width:720px){.metric-row{grid-template-columns:1fr;gap:8px}.metric-row .stats{justify-content:flex-start}.chart-mini{width:100%;height:60px}}
#tt{position:fixed;background:#1a1d26;color:#e2e6f0;border:1px solid #2d3346;padding:6px 10px;border-radius:4px;font-size:12px;pointer-events:none;opacity:0;transition:opacity .12s;z-index:100;white-space:nowrap}
#tt.show{opacity:1}
#tt .tt-date{color:#5a6070;font-size:10px;margin-bottom:2px}
#tt .tt-val{color:#e8ff47;font-weight:bold}
</style>
</head>
<body>
<h1>Live Dashboard ОсОО «Каравелла»</h1>
<div class="live">● Обновляется каждые 2 минуты &nbsp;·&nbsp; <a href="/admin" style="color:#5a6070;text-decoration:none">⚙ admin</a></div>
<div class="tabs">
  <button class="tab active" data-tab="today">Сегодня</button>
  <button class="tab" data-tab="week">Неделя</button>
  <button class="tab" data-tab="month">Месяц</button>
</div>
<div id="today" class="view">
  <div id="status"></div>
  <div id="today-data">Загрузка...</div>
</div>
<div id="week" class="view hidden">
  <div class="period" id="week-period"></div>
  <div id="week-data">Загрузка...</div>
</div>
<div id="month" class="view hidden">
  <div class="period" id="month-period"></div>
  <div id="month-data">Загрузка...</div>
</div>
<div id="tt"></div>
<script>
const SP=['Эльдана','Станислав','Мадина','Олег','Атай','Производство'];

function fmtDate(s){const d=new Date(s);return d.toLocaleDateString('ru-RU',{day:'2-digit',month:'2-digit'})}
function fmtNum(v){return Number(v).toLocaleString('ru-RU')}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c])}
function dn(map,k){return (map&&map[k])||k}

async function loadToday(){
  const data=await fetch('/api/today').then(r=>r.json());
  const rows=data.rows||[];
  const display=data.display_names||{};
  const textKeys=new Set(data.text_keys||[]);
  const sub=new Set(rows.map(r=>r.specialist));
  document.getElementById('status').innerHTML='<div class="card"><div class="name">Статус сдачи сегодня</div>'+SP.map(n=>`<span class="chip ${sub.has(n)?'done':'pending'}">${sub.has(n)?'✓':''} ${esc(n)}</span>`).join('')+'</div>';
  const bp={};
  rows.forEach(r=>{(bp[r.specialist]=bp[r.specialist]||[]).push(r)});
  let h='';
  for(const n of SP){
    if(!bp[n]) continue;
    h+=`<div class="card"><div class="name">${esc(n)}</div>`;
    bp[n].forEach(r=>{
      const isN=!textKeys.has(r.metric)&&!isNaN(parseFloat(r.value))&&isFinite(r.value);
      const valHtml=isN?`<span class="num">${fmtNum(r.value)}</span>`:esc(r.value);
      h+=`<div class="today-row"><span class="label">${esc(dn(display,r.metric))}</span><span class="value">${valHtml}</span></div>`;
    });
    h+='</div>';
  }
  document.getElementById('today-data').innerHTML=h||'<div class="card empty">Отчётов за сегодня пока нет</div>';
}

function getWorkingDays(startStr,endStr){
  const days=[];
  const d=new Date(startStr+'T00:00:00');
  const end=new Date(endStr+'T00:00:00');
  while(d<=end){
    const dow=d.getDay();
    if(dow!==0&&dow!==6){
      const y=d.getFullYear(),m=String(d.getMonth()+1).padStart(2,'0'),da=String(d.getDate()).padStart(2,'0');
      days.push(`${y}-${m}-${da}`);
    }
    d.setDate(d.getDate()+1);
  }
  return days;
}

function renderChart(metric,points,gid){
  if(!points||points.length===0)return '<div class="chart-empty">нет точек</div>';
  const W=120,H=44,PX=4,PY=4;
  const vals=points.map(p=>p.value);
  const maxV=Math.max(...vals);
  const minV=Math.min(0,Math.min(...vals));
  const range=(maxV-minV)||1;
  const n=points.length;
  const x=i=>n===1?W/2:PX+(i/(n-1))*(W-2*PX);
  const y=v=>H-PY-((v-minV)/range)*(H-2*PY);
  let line='',area='';
  if(n>=2){
    line=points.map((p,i)=>`${i===0?'M':'L'}${x(i).toFixed(1)} ${y(p.value).toFixed(1)}`).join(' ');
    area=line+` L${x(n-1).toFixed(1)} ${H-PY} L${x(0).toFixed(1)} ${H-PY} Z`;
  }
  const dots=points.map((p,i)=>`<circle class="dot" cx="${x(i).toFixed(1)}" cy="${y(p.value).toFixed(1)}" r="2.5" data-d="${p.date}" data-v="${p.value}" data-m="${esc(metric)}"/>`).join('');
  const grad=`g${gid}`;
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <defs><linearGradient id="${grad}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#e8ff47" stop-opacity=".4"/>
      <stop offset="100%" stop-color="#e8ff47" stop-opacity="0"/>
    </linearGradient></defs>
    ${area?`<path class="area" d="${area}" fill="url(#${grad})"/>`:''}
    ${line?`<path class="line" d="${line}"/>`:''}
    ${dots}
  </svg>`;
}

async function loadAggregate(period){
  const d=await fetch(`/api/aggregate?period=${period}`).then(r=>r.json());
  document.getElementById(`${period}-period`).textContent=`Период: ${fmtDate(d.start)} – ${fmtDate(d.end)}`;
  const display=d.display_names||{};
  let h='';let gid=0;
  for(const n of SP){
    const b=d.specialists[n];
    if(!b){h+=`<div class="card"><div class="name">${esc(n)}<span class="days">нет данных</span></div></div>`;continue}
    const metricsArr=Object.entries(b.metrics);
    h+=`<div class="card"><div class="name">${esc(n)}<span class="days">${b.days_submitted} ${b.days_submitted===1?'день':'дн.'} с отчётами</span></div>`;
    if(metricsArr.length){
      const prevSp=(d.prev_specialists&&d.prev_specialists[n])||{};
      const allDays=getWorkingDays(d.start,d.end);
      metricsArr.forEach(([m,v])=>{
        const series=(b.series&&b.series[m])||[];
        const byDate={};series.forEach(p=>{byDate[p.date]=p.value});
        const avg=(b.averages&&b.averages[m])??null;
        const prev=prevSp[m]??null;
        let trend='';
        if(prev===null||prev===0){
          if(v>0)trend=`<span class="trend new">NEW</span>`;
        }else{
          const pct=((v-prev)/prev)*100;
          const abs=Math.abs(pct);
          if(abs<1)trend=`<span class="trend flat">· 0%</span>`;
          else if(pct>0)trend=`<span class="trend up">↑ ${abs.toFixed(0)}%</span>`;
          else trend=`<span class="trend down">↓ ${abs.toFixed(0)}%</span>`;
        }
        const avgHtml=avg!==null?`<span class="avg">~${fmtNum(avg)}/день</span>`:'';
        const daysHtml=allDays.map(dd=>{
          const has=byDate[dd]!==undefined;
          const val=has?fmtNum(byDate[dd]):'—';
          return `<div class="day-cell${has?'':' empty'}"><div class="d">${fmtDate(dd)}</div><div class="v">${val}</div></div>`;
        }).join('');
        const lbl=dn(display,m);
        h+=`<div class="metric-row">
          <span class="label">${esc(lbl)}</span>
          <div class="days">${daysHtml}</div>
          <div class="stats">${avgHtml}${trend}<span class="total">${fmtNum(v)}</span></div>
          <div class="chart-mini">${renderChart(lbl,series,`${period}-${gid++}`)}</div>
        </div>`;
      });
    }else{
      h+='<div class="empty" style="padding:8px">Нет числовых метрик</div>';
    }
    if(b.comments&&b.comments.length){
      h+=`<details><summary>💬 Комментарии (${b.comments.length})</summary>`;
      b.comments.slice().sort((a,b)=>a.date<b.date?1:-1).forEach(c=>{
        h+=`<div class="cmt"><span class="cmt-date">${fmtDate(c.date)}</span><span class="cmt-metric">${esc(c.metric)}</span>${esc(c.value)}</div>`;
      });
      h+='</details>';
    }
    h+='</div>';
  }
  document.getElementById(`${period}-data`).innerHTML=h;
  bindTooltips(period);
}

function bindTooltips(period){
  const tt=document.getElementById('tt');
  document.querySelectorAll(`#${period} .dot`).forEach(el=>{
    el.addEventListener('mouseenter',e=>{
      const d=el.getAttribute('data-d'),v=el.getAttribute('data-v'),m=el.getAttribute('data-m');
      tt.innerHTML=`<div class="tt-date">${fmtDate(d)} · ${esc(m)}</div><div class="tt-val">${fmtNum(v)}</div>`;
      tt.classList.add('show');
    });
    el.addEventListener('mousemove',e=>{
      tt.style.left=(e.clientX+12)+'px';
      tt.style.top=(e.clientY+12)+'px';
    });
    el.addEventListener('mouseleave',()=>tt.classList.remove('show'));
  });
}

function showTab(tab){
  document.querySelectorAll('.tab').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab));
  document.querySelectorAll('.view').forEach(v=>v.classList.toggle('hidden',v.id!==tab));
  if(tab==='today')loadToday();
  else loadAggregate(tab);
}

document.querySelectorAll('.tab').forEach(b=>b.addEventListener('click',()=>showTab(b.dataset.tab)));
loadToday();
setInterval(()=>{const a=document.querySelector('.tab.active').dataset.tab;if(a==='today')loadToday();else loadAggregate(a)},120000);
</script>
</body>
</html>"""

@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)

ADMIN_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin · Live Dashboard ОсОО «Каравелла»</title>
<style>
body{background:#0a0c10;color:#e2e6f0;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;padding:24px;margin:0;max-width:1100px;margin:0 auto}
h1{color:#e8ff47;font-size:24px;margin:0 0 8px}
.sub{color:#5a6070;font-size:12px;margin-bottom:24px}
.sub a{color:#47c8ff;text-decoration:none}
.token-bar{background:#11141b;border:1px solid #232838;padding:14px 18px;border-radius:6px;margin-bottom:24px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.token-bar label{color:#9097a8;font-size:12px}
.token-bar input{background:#0d1017;border:1px solid #2d3346;color:#e2e6f0;padding:8px 10px;border-radius:4px;font-family:inherit;font-size:13px;flex:1;min-width:180px}
.token-bar button{background:#e8ff47;color:#0a0c10;border:none;padding:8px 16px;border-radius:4px;font-weight:600;cursor:pointer;font-family:inherit;font-size:13px}
.token-bar button:hover{filter:brightness(1.1)}
.toggle-hidden{display:flex;align-items:center;gap:6px;color:#9097a8;font-size:12px;margin-bottom:16px;cursor:pointer;user-select:none}
.toggle-hidden input{accent-color:#e8ff47}
.adm-card{background:#11141b;border:1px solid #232838;border-radius:6px;padding:18px 18px 12px;margin-bottom:18px}
.adm-card h2{color:#47c8ff;font-size:16px;margin:0 0 14px}
.adm-row{display:grid;grid-template-columns:auto 1fr 2fr auto auto auto;gap:8px;align-items:center;padding:8px 0;border-top:1px solid #1a1d26}
.adm-row:first-of-type{border-top:none}
.adm-row.inactive{opacity:.5}
.adm-row .move{display:flex;flex-direction:column;gap:2px}
.adm-row .move button{background:#0d1017;border:1px solid #232838;color:#9097a8;width:24px;height:14px;font-size:9px;cursor:pointer;border-radius:2px;padding:0;line-height:1}
.adm-row .move button:hover{color:#e8ff47;border-color:#2d3346}
.adm-row input[type=text]{background:#0d1017;border:1px solid #1a1d26;color:#e2e6f0;padding:6px 8px;border-radius:3px;font-family:inherit;font-size:13px;width:100%;box-sizing:border-box}
.adm-row input[type=text]:focus{outline:none;border-color:#47c8ff}
.adm-row .saved{color:#4ade80;font-size:10px;opacity:0;transition:opacity .15s}
.adm-row .saved.show{opacity:1}
.adm-row .text-toggle{display:flex;align-items:center;gap:4px;color:#5a6070;font-size:11px;cursor:pointer;user-select:none}
.adm-row .text-toggle input{accent-color:#e8ff47;cursor:pointer}
.adm-row .del,.adm-row .restore{background:none;border:1px solid #232838;color:#5a6070;padding:5px 9px;border-radius:3px;cursor:pointer;font-size:12px;font-family:inherit}
.adm-row .del:hover{color:#ef6464;border-color:rgba(239,100,100,.3)}
.adm-row .restore{color:#4ade80;border-color:rgba(74,222,128,.3)}
.adm-row .restore:hover{filter:brightness(1.2)}
.adm-add{display:grid;grid-template-columns:1fr 2fr auto auto;gap:8px;margin-top:14px;padding-top:14px;border-top:1px dashed #232838;align-items:center}
.adm-add input{background:#0d1017;border:1px solid #1a1d26;color:#e2e6f0;padding:7px 9px;border-radius:3px;font-family:inherit;font-size:13px;box-sizing:border-box}
.adm-add input::placeholder{color:#3d4350}
.adm-add input:focus{outline:none;border-color:#47c8ff}
.adm-add button{background:#e8ff47;color:#0a0c10;border:none;padding:7px 14px;border-radius:3px;font-weight:600;cursor:pointer;font-family:inherit;font-size:13px}
.adm-add button:hover{filter:brightness(1.1)}
.adm-add label{color:#5a6070;font-size:11px;display:flex;align-items:center;gap:4px;cursor:pointer;user-select:none}
.empty-msg{color:#5a6070;text-align:center;padding:40px 0;font-size:14px}
.col-h{display:grid;grid-template-columns:auto 1fr 2fr auto auto auto;gap:8px;color:#5a6070;font-size:10px;text-transform:uppercase;letter-spacing:.5px;padding:0 0 8px;border-bottom:1px solid #1a1d26;margin-bottom:6px}
.col-h span:nth-child(1){visibility:hidden}
.hidden{display:none}
@media (max-width:720px){.adm-row,.adm-add,.col-h{grid-template-columns:1fr}.col-h{display:none}}
</style>
</head>
<body>
<h1>⚙ Управление показателями</h1>
<div class="sub">«Каравелла» · <a href="/">← на дашборд</a></div>
<div class="token-bar" id="tokenBar">
  <label>Введите admin-token (из Railway → Variables → ADMIN_TOKEN):</label>
  <input id="tokenInput" type="password" placeholder="токен" autofocus>
  <button onclick="saveToken()">Войти</button>
</div>
<label class="toggle-hidden hidden" id="hiddenToggleWrap">
  <input type="checkbox" id="showHidden" onchange="render()"> показывать скрытые
</label>
<div id="content"></div>
<script>
let TOKEN = sessionStorage.getItem('admin_token') || '';
let DATA = null;

function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c])}

async function api(path, opts={}){
  const headers = Object.assign({'Content-Type':'application/json','X-Admin-Token':TOKEN}, opts.headers||{});
  const r = await fetch(path, Object.assign({}, opts, {headers}));
  if(r.status === 401){
    sessionStorage.removeItem('admin_token'); TOKEN=''; showLogin('неверный токен');
    throw new Error('unauthorized');
  }
  if(!r.ok) throw new Error('http '+r.status);
  return r.json();
}

function showLogin(msg){
  document.getElementById('tokenBar').classList.remove('hidden');
  document.getElementById('hiddenToggleWrap').classList.add('hidden');
  document.getElementById('content').innerHTML='';
  if(msg) document.getElementById('tokenBar').querySelector('label').textContent='⚠ '+msg+'. Введите admin-token:';
}

function saveToken(){
  const t = document.getElementById('tokenInput').value.trim();
  if(!t) return;
  TOKEN = t;
  sessionStorage.setItem('admin_token', t);
  load();
}

async function load(){
  try{
    DATA = await api('/admin/api/config');
    document.getElementById('tokenBar').classList.add('hidden');
    document.getElementById('hiddenToggleWrap').classList.remove('hidden');
    render();
  }catch(e){ /* showLogin already handled */ }
}

function render(){
  if(!DATA) return;
  const showHidden = document.getElementById('showHidden').checked;
  const bySpec = {};
  DATA.metrics.forEach(m=>{(bySpec[m.specialist]=bySpec[m.specialist]||[]).push(m)});
  let h='';
  for(const sp of DATA.specialists){
    const items = (bySpec[sp]||[]).filter(m => showHidden || m.is_active);
    h += `<div class="adm-card"><h2>${esc(sp)}</h2>`;
    h += '<div class="col-h"><span></span><span>название</span><span>текст вопроса</span><span>тип</span><span></span><span></span></div>';
    if(items.length===0){
      h += '<div class="empty-msg">Нет показателей</div>';
    } else {
      items.forEach(m=>{
        const inactive = m.is_active ? '' : ' inactive';
        h += `<div class="adm-row${inactive}" data-id="${m.id}">
          <div class="move">
            <button onclick="move(${m.id},'up')">▲</button>
            <button onclick="move(${m.id},'down')">▼</button>
          </div>
          <input type="text" value="${esc(m.display_name)}" data-field="display" onblur="upd(${m.id},'display',this.value,this)">
          <input type="text" value="${esc(m.question_text)}" data-field="question" onblur="upd(${m.id},'question',this.value,this)">
          <label class="text-toggle"><input type="checkbox" ${m.is_text?'checked':''} onchange="upd(${m.id},'is_text',this.checked,this)"> текст</label>
          <span class="saved">✓ saved</span>
          ${m.is_active
            ? `<button class="del" onclick="del(${m.id})">🗑</button>`
            : `<button class="restore" onclick="restore(${m.id})">↩ восстановить</button>`}
        </div>`;
      });
    }
    h += `<div class="adm-add">
      <input type="text" placeholder="название (напр. лиды)" id="new-display-${esc(sp)}">
      <input type="text" placeholder="текст вопроса (с эмодзи)" id="new-question-${esc(sp)}">
      <label><input type="checkbox" id="new-text-${esc(sp)}"> текст</label>
      <button onclick="add('${esc(sp)}')">+ добавить</button>
    </div>`;
    h += '</div>';
  }
  document.getElementById('content').innerHTML = h;
}

function flashSaved(el){
  const row = el.closest('.adm-row'); if(!row) return;
  const s = row.querySelector('.saved'); if(!s) return;
  s.classList.add('show'); setTimeout(()=>s.classList.remove('show'), 900);
}

async function upd(id, field, value, el){
  try{
    await api(`/admin/api/metric/${id}/update`, {method:'POST', body: JSON.stringify({[field]: value})});
    flashSaved(el);
    const m = DATA.metrics.find(x=>x.id===id);
    if(m){
      if(field==='display') m.display_name = value;
      if(field==='question') m.question_text = value;
      if(field==='is_text') m.is_text = value;
    }
  }catch(e){alert('Не удалось сохранить: '+e.message)}
}

async function del(id){
  if(!confirm('Скрыть этот показатель? Старые данные сохранятся, но в опросе он больше не появится.')) return;
  try{ await api(`/admin/api/metric/${id}/delete`, {method:'POST'}); load(); }
  catch(e){alert(e.message)}
}

async function restore(id){
  try{ await api(`/admin/api/metric/${id}/restore`, {method:'POST'}); load(); }
  catch(e){alert(e.message)}
}

async function move(id, direction){
  try{ await api(`/admin/api/metric/${id}/move`, {method:'POST', body: JSON.stringify({direction})}); load(); }
  catch(e){alert(e.message)}
}

async function add(specialist){
  const display = document.getElementById('new-display-'+specialist).value.trim();
  const question = document.getElementById('new-question-'+specialist).value.trim();
  const is_text = document.getElementById('new-text-'+specialist).checked;
  if(!display || !question){alert('Заполните название и текст вопроса');return}
  try{
    await api('/admin/api/metric', {method:'POST', body: JSON.stringify({specialist, display, question, is_text})});
    load();
  }catch(e){alert(e.message)}
}

if(TOKEN) load(); else showLogin('');
</script>
</body>
</html>"""

@app.route("/api/today")
def api_today():
    rows = get_today_reports()
    display, text_keys = get_config_lookups()
    return jsonify({
        "rows": [dict(r) for r in rows],
        "display_names": display,
        "text_keys": list(text_keys),
    })

@app.route("/api/aggregate")
def api_aggregate():
    period = request.args.get("period", "week")
    if period not in ("week", "month"):
        period = "week"
    start, end = period_range(period)
    display, text_keys = get_config_lookups()
    rows = get_period_reports(start, end)
    data = aggregate_reports(rows, text_keys)
    length = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=length - 1)
    prev_rows = get_period_reports(prev_start, prev_end)
    prev_data = aggregate_reports(prev_rows, text_keys)
    prev_metrics = {sp: b["metrics"] for sp, b in prev_data.items()}
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
    })

@app.route("/health")
def health():
    return "ok"

def _check_admin_token():
    if not ADMIN_TOKEN:
        abort(503, "Admin not configured")
    token = request.args.get("token") or request.headers.get("X-Admin-Token") or ""
    if not hmac.compare_digest(token, ADMIN_TOKEN):
        abort(401, "Invalid token")

def _gen_metric_key(specialist, display, cur):
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

@app.route("/admin")
def admin_page():
    if not ADMIN_TOKEN:
        return "Admin disabled. Set ADMIN_TOKEN env var on Railway and redeploy.", 503
    return render_template_string(ADMIN_HTML)

@app.route("/admin/api/config")
def admin_config():
    _check_admin_token()
    rows = get_admin_config()
    return jsonify({
        "specialists": SPECIALIST_ORDER,
        "metrics": [dict(r) for r in rows],
    })

@app.route("/admin/api/metric", methods=["POST"])
def admin_create():
    _check_admin_token()
    data = request.get_json(silent=True) or {}
    specialist = (data.get("specialist") or "").strip()
    display = (data.get("display") or "").strip()
    question = (data.get("question") or "").strip()
    is_text = bool(data.get("is_text"))
    if specialist not in SPECIALIST_ORDER:
        abort(400, "Unknown specialist")
    if not display or not question:
        abort(400, "display and question are required")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM metrics_config WHERE specialist = %s",
                (specialist,),
            )
            new_pos = cur.fetchone()[0]
            key = _gen_metric_key(specialist, display, cur)
            cur.execute(
                "INSERT INTO metrics_config (specialist, metric_key, display_name, question_text, position, is_text, is_active) "
                "VALUES (%s,%s,%s,%s,%s,%s,TRUE) RETURNING id",
                (specialist, key, display, question, new_pos, is_text),
            )
            new_id = cur.fetchone()[0]
        conn.commit()
    return jsonify({"id": new_id, "metric_key": key, "ok": True})

@app.route("/admin/api/metric/<int:metric_id>/update", methods=["POST"])
def admin_update(metric_id):
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
    if not fields:
        abort(400, "no fields to update")
    sets = ", ".join(f"{k} = %s" for k in fields)
    params = list(fields.values()) + [metric_id]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE metrics_config SET {sets} WHERE id = %s", params)
            ok = cur.rowcount > 0
        conn.commit()
    return jsonify({"ok": ok})

@app.route("/admin/api/metric/<int:metric_id>/delete", methods=["POST"])
def admin_delete(metric_id):
    _check_admin_token()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE metrics_config SET is_active = FALSE WHERE id = %s", (metric_id,))
            ok = cur.rowcount > 0
        conn.commit()
    return jsonify({"ok": ok})

@app.route("/admin/api/metric/<int:metric_id>/restore", methods=["POST"])
def admin_restore(metric_id):
    _check_admin_token()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE metrics_config SET is_active = TRUE WHERE id = %s", (metric_id,))
            ok = cur.rowcount > 0
        conn.commit()
    return jsonify({"ok": ok})

@app.route("/admin/api/metric/<int:metric_id>/move", methods=["POST"])
def admin_move(metric_id):
    _check_admin_token()
    direction = (request.get_json(silent=True) or {}).get("direction")
    if direction not in ("up", "down"):
        abort(400, "direction must be 'up' or 'down'")
    with get_conn() as conn:
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
        conn.commit()
    return jsonify({"ok": True})

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
        time=dtime(hour=9, minute=0, tzinfo=BISHKEK),
        days=(0, 1, 2, 3, 4),
    )
    init_db()
    logger.info("Бот запущен")
    application.run_polling()

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    run_bot()
