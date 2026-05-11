"""Заполнение БД случайными демо-данными для тестов дашборда.

Запуск:
    DATABASE_URL=postgres://... python seed.py             # 14 рабочих дней
    python seed.py --days 30                               # 30 рабочих дней
    python seed.py --clear                                 # очистить и засеять заново
    python seed.py --days 21 --clear --skip-rate 0.15      # доля пропусков

По умолчанию каждый специалист сдаёт отчёт с вероятностью 80% в день
(чтобы дашборд показывал реалистичные пропуски).
"""
import os
import random
import argparse
from datetime import datetime, timedelta, time as dtime
import psycopg2
import pytz

DATABASE_URL = os.environ["DATABASE_URL"]
BISHKEK = pytz.timezone("Asia/Bishkek")

NUMERIC = "NUM"
TEXT = "TEXT"

SPECIALISTS_METRICS = {
    "Эльдана": [
        ("заявки", NUMERIC, 5, 15),
        ("письма", NUMERIC, 20, 40),
        ("рассылки", NUMERIC, 1, 5),
        ("комментарий", TEXT, None, None),
    ],
    "Станислав": [
        ("контакты", NUMERIC, 3, 12),
        ("кп", NUMERIC, 1, 6),
        ("договора", NUMERIC, 0, 3),
        ("объекты_работа", NUMERIC, 2, 8),
        ("объекты_разраб", NUMERIC, 1, 5),
        ("доход", NUMERIC, 0, 50000),
        ("комментарий", TEXT, None, None),
    ],
    "Мадина": [
        ("контакты", NUMERIC, 5, 15),
        ("пакеты", NUMERIC, 0, 5),
        ("договора", NUMERIC, 0, 2),
        ("объекты_работа", NUMERIC, 2, 6),
        ("комментарий", TEXT, None, None),
    ],
    "Олег": [
        ("контакты", NUMERIC, 4, 12),
        ("кп", NUMERIC, 1, 5),
        ("клиенты_работа", NUMERIC, 3, 10),
        ("комментарий", TEXT, None, None),
    ],
    "Атай": [
        ("тендеры_найдено", NUMERIC, 2, 10),
        ("заявки_подготовл", NUMERIC, 1, 6),
        ("заявки_подано", NUMERIC, 1, 5),
        ("заявки_отклонено", NUMERIC, 0, 2),
        ("тендеры_выиграно", NUMERIC, 0, 2),
        ("сумма_подано", NUMERIC, 100000, 2000000),
        ("комментарий", TEXT, None, None),
    ],
    "Производство": [
        ("сделано", TEXT, None, None),
        ("план_завтра", TEXT, None, None),
        ("проблемы", TEXT, None, None),
    ],
}

COMMENTS_POOL = [
    "хороший день, всё по плану",
    "встреча с потенциальным клиентом",
    "подписали договор с заводом",
    "переговоры идут вторую неделю",
    "звонил постоянный заказчик",
    "отправили коммерческое предложение",
    "встреча перенесена на следующую неделю",
    "клиент думает, ждём решения",
    "-", "-", "-", "-", "-",
]
PRODUCTION_DONE = [
    "покрасили партию деталей",
    "собрали корпус для заказа №142",
    "тестируем новый образец",
    "отправили партию заказчику",
    "наладили станок после ремонта",
    "приёмка нового оборудования",
]
PRODUCTION_PLAN = [
    "сборка корпуса заказа №143",
    "покраска и упаковка",
    "доставка готового заказа",
    "тестирование прототипа",
    "плановое ТО оборудования",
]
PRODUCTION_PROBLEMS = ["-", "-", "-", "-", "сломался компрессор", "не хватает листового металла", "задержка от поставщика"]


def is_working(d):
    return d.weekday() < 5


def gen_value(specialist, metric, kind, lo, hi):
    if kind == NUMERIC:
        return str(random.randint(lo, hi))
    if specialist == "Производство":
        if metric == "сделано":
            return random.choice(PRODUCTION_DONE)
        if metric == "план_завтра":
            return random.choice(PRODUCTION_PLAN)
        return random.choice(PRODUCTION_PROBLEMS)
    return random.choice(COMMENTS_POOL)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=14, help="кол-во рабочих дней (по умолчанию 14)")
    p.add_argument("--clear", action="store_true", help="очистить таблицу reports перед вставкой")
    p.add_argument("--wipe-only", action="store_true",
                   help="ТОЛЬКО очистить ВСЕ отчёты и выйти (без вставки демо-данных)")
    p.add_argument("--wipe-today", action="store_true",
                   help="ТОЛЬКО удалить отчёты за сегодня (Bishkek time)")
    p.add_argument("--wipe-date", type=str, default=None, metavar="YYYY-MM-DD",
                   help="ТОЛЬКО удалить отчёты за указанную дату")
    p.add_argument("--keep-after", type=str, default=None, metavar="\"YYYY-MM-DD HH:MM\"",
                   help="Удалить ВСЁ кроме отчётов после указанной даты-времени (Bishkek)")
    p.add_argument("--skip-rate", type=float, default=0.2, help="вероятность пропуска у специалиста (0.2 = 20%)")
    args = p.parse_args()

    print(f"→ Подключаюсь к БД...")
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id SERIAL PRIMARY KEY,
            date DATE NOT NULL, time TIME NOT NULL,
            specialist TEXT NOT NULL, metric TEXT NOT NULL, value TEXT NOT NULL
        )
    """)

    if args.keep_after:
        try:
            cutoff = datetime.strptime(args.keep_after.strip(), "%Y-%m-%d %H:%M")
        except ValueError:
            print(f"❌ Неверный формат: {args.keep_after!r}. Используйте \"YYYY-MM-DD HH:MM\"")
            cur.close(); conn.close()
            return
        cutoff_date = cutoff.date()
        cutoff_time = cutoff.time()
        # Count what will be deleted (everything strictly before cutoff datetime)
        cur.execute(
            "SELECT COUNT(*) FROM reports "
            "WHERE date < %s OR (date = %s AND time < %s)",
            (cutoff_date, cutoff_date, cutoff_time),
        )
        to_delete = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM reports "
            "WHERE date > %s OR (date = %s AND time >= %s)",
            (cutoff_date, cutoff_date, cutoff_time),
        )
        to_keep = cur.fetchone()[0]
        cur.execute(
            "DELETE FROM reports "
            "WHERE date < %s OR (date = %s AND time < %s)",
            (cutoff_date, cutoff_date, cutoff_time),
        )
        conn.commit()
        cur.close()
        conn.close()
        print(f"🧹 Удалено {to_delete} отчётов до {args.keep_after} (Bishkek)")
        print(f"✅ Сохранено {to_keep} отчётов начиная с {args.keep_after}")
        print("ℹ️  Сотрудники, метрики и настройки не тронуты.")
        return

    if args.wipe_today or args.wipe_date:
        if args.wipe_today:
            target = datetime.now(BISHKEK).date()
        else:
            try:
                target = datetime.strptime(args.wipe_date, "%Y-%m-%d").date()
            except ValueError:
                print(f"❌ Неверный формат даты: {args.wipe_date}. Используйте YYYY-MM-DD")
                cur.close(); conn.close()
                return
        cur.execute("SELECT COUNT(*) FROM reports WHERE date = %s", (target,))
        before = cur.fetchone()[0]
        cur.execute("DELETE FROM reports WHERE date = %s", (target,))
        conn.commit()
        cur.close()
        conn.close()
        print(f"🧹 Удалено {before} отчётов за {target.isoformat()}")
        print("ℹ️  Прошлые дни, сотрудники и настройки не тронуты.")
        return

    if args.wipe_only:
        cur.execute("SELECT COUNT(*) FROM reports")
        before = cur.fetchone()[0]
        cur.execute("TRUNCATE reports RESTART IDENTITY")
        conn.commit()
        cur.close()
        conn.close()
        print(f"🧹 Очищено {before} отчётов. Таблица reports пуста.")
        print("ℹ️  Настройки метрик, сотрудники и время опроса сохранены — обнулена только история.")
        return

    if args.clear:
        cur.execute("TRUNCATE reports RESTART IDENTITY")
        print("→ Старые отчёты удалены")

    today = datetime.now(BISHKEK).date()
    inserted = 0
    skipped_specialist_days = 0
    days_back = 0
    collected = 0

    while collected < args.days:
        d = today - timedelta(days=days_back)
        days_back += 1
        if days_back > args.days * 5:
            break
        if not is_working(d):
            continue
        collected += 1
        for specialist, metrics in SPECIALISTS_METRICS.items():
            if random.random() < args.skip_rate:
                skipped_specialist_days += 1
                continue
            t = dtime(hour=random.randint(9, 18), minute=random.randint(0, 59))
            for metric, kind, lo, hi in metrics:
                v = gen_value(specialist, metric, kind, lo, hi)
                cur.execute(
                    "INSERT INTO reports (date,time,specialist,metric,value) VALUES (%s,%s,%s,%s,%s)",
                    (d, t, specialist, metric, v),
                )
                inserted += 1
    conn.commit()
    cur.close()
    conn.close()
    print(f"✅ Вставлено {inserted} строк, пропущено {skipped_specialist_days} «спец-дней»")
    print(f"📅 Период: последние {collected} рабочих дней (до {today.isoformat()})")


if __name__ == "__main__":
    main()
