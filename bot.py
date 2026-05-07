import os
import logging
import threading
from datetime import datetime, time as dtime, date as ddate, timedelta
from flask import Flask, render_template_string, jsonify, request
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
TEXT_METRICS = {"комментарий", "сделано", "план_завтра", "проблемы"}
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

def aggregate_reports(rows):
    result = {}
    for r in rows:
        sp = r["specialist"]
        bucket = result.setdefault(sp, {"metrics": {}, "comments": [], "_days": set(), "_series": {}})
        bucket["_days"].add(r["date"])
        metric = r["metric"]
        value = (r["value"] or "").strip()
        num = None if metric in TEXT_METRICS else parse_number(value)
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
<title>Дашборд</title>
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
.metric-row{display:flex;justify-content:space-between;align-items:baseline;margin:16px 0 6px;padding:0 4px;gap:8px;flex-wrap:wrap}
.metric-row .label{color:#9097a8;font-size:13px;text-transform:lowercase;flex:1;min-width:120px}
.metric-row .right{display:flex;align-items:baseline;gap:10px}
.metric-row .avg{color:#5a6070;font-size:11px}
.metric-row .total{color:#e8ff47;font-size:18px;font-weight:bold}
.trend{display:inline-flex;align-items:center;gap:3px;font-size:11px;padding:2px 7px;border-radius:10px;font-weight:600}
.trend.up{color:#4ade80;background:rgba(74,222,128,.12);border:1px solid rgba(74,222,128,.25)}
.trend.down{color:#ef6464;background:rgba(239,100,100,.1);border:1px solid rgba(239,100,100,.25)}
.trend.flat{color:#5a6070;background:rgba(90,96,112,.1);border:1px solid #232838}
.trend.new{color:#47c8ff;background:rgba(71,200,255,.1);border:1px solid rgba(71,200,255,.25)}
.chart{position:relative;background:#0d1017;border:1px solid #1a1d26;border-radius:6px;padding:12px 8px 6px;transition:transform .18s ease,border-color .18s ease,box-shadow .18s ease}
.chart:hover{transform:scale(1.02);border-color:#2d3346;box-shadow:0 8px 24px rgba(0,0,0,.4)}
.chart svg{width:100%;height:90px;display:block;overflow:visible}
.chart .axis{display:flex;justify-content:space-between;color:#5a6070;font-size:10px;margin-top:4px;padding:0 4px}
.chart .dot{fill:#e8ff47;stroke:#0d1017;stroke-width:2;cursor:pointer;transition:r .15s ease}
.chart .dot:hover{r:5}
.chart .line{fill:none;stroke:#e8ff47;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.chart .area{opacity:.85}
.chart-empty{color:#5a6070;font-size:11px;text-align:center;padding:18px 0}
#tt{position:fixed;background:#1a1d26;color:#e2e6f0;border:1px solid #2d3346;padding:6px 10px;border-radius:4px;font-size:12px;pointer-events:none;opacity:0;transition:opacity .12s;z-index:100;white-space:nowrap}
#tt.show{opacity:1}
#tt .tt-date{color:#5a6070;font-size:10px;margin-bottom:2px}
#tt .tt-val{color:#e8ff47;font-weight:bold}
</style>
</head>
<body>
<h1>LIVE ДАШБОРД</h1>
<div class="live">● Обновляется каждые 2 минуты</div>
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
const TEXT_METRICS=new Set(['комментарий','сделано','план_завтра','проблемы']);

function fmtDate(s){const d=new Date(s);return d.toLocaleDateString('ru-RU',{day:'2-digit',month:'2-digit'})}
function fmtNum(v){return Number(v).toLocaleString('ru-RU')}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c])}

async function loadToday(){
  const t=await fetch('/api/today').then(r=>r.json());
  const sub=new Set(t.map(r=>r.specialist));
  document.getElementById('status').innerHTML='<div class="card"><div class="name">Статус сдачи сегодня</div>'+SP.map(n=>`<span class="chip ${sub.has(n)?'done':'pending'}">${sub.has(n)?'✓':''} ${esc(n)}</span>`).join('')+'</div>';
  const bp={};
  t.forEach(r=>{(bp[r.specialist]=bp[r.specialist]||[]).push(r)});
  let h='';
  for(const n of SP){
    if(!bp[n]) continue;
    h+=`<div class="card"><div class="name">${esc(n)}</div><table>`;
    bp[n].forEach(r=>{
      const isN=!TEXT_METRICS.has(r.metric)&&!isNaN(parseFloat(r.value))&&isFinite(r.value);
      h+=`<tr><td style="color:#5a6070">${esc(r.metric)}</td><td>${isN?`<span class="num">${fmtNum(r.value)}</span>`:esc(r.value)}</td></tr>`;
    });
    h+='</table></div>';
  }
  document.getElementById('today-data').innerHTML=h||'<div class="card empty">Отчётов за сегодня пока нет</div>';
}

function renderChart(metric, points, gid){
  if(!points||points.length===0)return '<div class="chart-empty">Нет данных для графика</div>';
  const W=600,H=90,PX=8,PY=12;
  const vals=points.map(p=>p.value);
  const maxV=Math.max(...vals);
  const minV=Math.min(0,Math.min(...vals));
  const range=(maxV-minV)||1;
  const n=points.length;
  const x=i=>n===1?W/2:PX+(i/(n-1))*(W-2*PX);
  const y=v=>H-PY-((v-minV)/range)*(H-2*PY);
  let line='',area='',dots='';
  if(n>=2){
    line=points.map((p,i)=>`${i===0?'M':'L'}${x(i).toFixed(1)} ${y(p.value).toFixed(1)}`).join(' ');
    area=line+` L${x(n-1).toFixed(1)} ${H-PY} L${x(0).toFixed(1)} ${H-PY} Z`;
  }
  dots=points.map((p,i)=>`<circle class="dot" cx="${x(i).toFixed(1)}" cy="${y(p.value).toFixed(1)}" r="3.5" data-d="${p.date}" data-v="${p.value}" data-m="${esc(metric)}"/>`).join('');
  const firstDate=fmtDate(points[0].date);
  const lastDate=fmtDate(points[n-1].date);
  const grad=`g${gid}`;
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <defs><linearGradient id="${grad}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#e8ff47" stop-opacity=".35"/>
      <stop offset="100%" stop-color="#e8ff47" stop-opacity="0"/>
    </linearGradient></defs>
    ${area?`<path class="area" d="${area}" fill="url(#${grad})"/>`:''}
    ${line?`<path class="line" d="${line}"/>`:''}
    ${dots}
  </svg><div class="axis"><span>${firstDate}</span>${n>1?`<span>${lastDate}</span>`:''}</div>`;
}

async function loadAggregate(period){
  const d=await fetch(`/api/aggregate?period=${period}`).then(r=>r.json());
  document.getElementById(`${period}-period`).textContent=`Период: ${fmtDate(d.start)} – ${fmtDate(d.end)}`;
  let h='';let gid=0;
  for(const n of SP){
    const b=d.specialists[n];
    if(!b){h+=`<div class="card"><div class="name">${esc(n)}<span class="days">нет данных</span></div></div>`;continue}
    const metricsArr=Object.entries(b.metrics);
    h+=`<div class="card"><div class="name">${esc(n)}<span class="days">${b.days_submitted} ${b.days_submitted===1?'день':'дн.'} с отчётами</span></div>`;
    if(metricsArr.length){
      const prevSp=(d.prev_specialists&&d.prev_specialists[n])||{};
      metricsArr.forEach(([m,v])=>{
        const series=(b.series&&b.series[m])||[];
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
        h+=`<div class="metric-row"><span class="label">${esc(m)}</span><span class="right">${avgHtml}${trend}<span class="total">${fmtNum(v)}</span></span></div>`;
        h+=`<div class="chart">${renderChart(m,series,`${period}-${gid++}`)}</div>`;
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

@app.route("/api/today")
def api_today():
    rows = get_today_reports()
    return jsonify([dict(r) for r in rows])

@app.route("/api/aggregate")
def api_aggregate():
    period = request.args.get("period", "week")
    if period not in ("week", "month"):
        period = "week"
    start, end = period_range(period)
    rows = get_period_reports(start, end)
    data = aggregate_reports(rows)
    length = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=length - 1)
    prev_rows = get_period_reports(prev_start, prev_end)
    prev_data = aggregate_reports(prev_rows)
    prev_metrics = {sp: b["metrics"] for sp, b in prev_data.items()}
    return jsonify({
        "period": period,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "prev_start": prev_start.isoformat(),
        "prev_end": prev_end.isoformat(),
        "specialists": data,
        "prev_specialists": prev_metrics,
    })

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
