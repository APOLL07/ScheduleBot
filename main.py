# -*- coding: utf-8 -*-
import re
import asyncio
import os
import json
import psycopg2
import psycopg2.extras
import pytz
from flask import Flask, request as flask_request, abort, jsonify
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ChatAction
from datetime import datetime, time, timedelta
from asgiref.wsgi import WsgiToAsgi
from contextlib import asynccontextmanager
from flask_apscheduler import APScheduler
from dotenv import load_dotenv

import cohere

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
COHERE_API_KEY = os.environ.get("COHERE_API_KEY")

admin_id_raw = os.environ.get("ADMIN_ID")
if not admin_id_raw:
    print("КРИТИЧНА ПОМИЛКА: ADMIN_ID не знайдено в .env файлі!")
    exit(1)
ADMIN_ID = int(admin_id_raw)

admin_id_2_raw = os.environ.get("ADMIN_ID_2")
ADMIN_ID_2 = int(admin_id_2_raw) if admin_id_2_raw else None

ADMIN_IDS = {ADMIN_ID} | ({ADMIN_ID_2} if ADMIN_ID_2 else set())

REMIND_BEFORE_MINUTES = 10
TIMEZONE = pytz.timezone('Europe/Kiev')

REFERENCE_DATE = datetime(2025, 2, 24).date()
REFERENCE_WEEK_TYPE = "парний"

DAY_OF_WEEK_UKR = {0: "понеділок", 1: "вівторок", 2: "середа", 3: "четвер", 4: "п'ятниця", 5: "субота", 6: "неділя"}
DAY_ORDER_LIST = ["понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"]
AI_TO_DB_DAYS = {"Monday": "понеділок", "Tuesday": "вівторок", "Wednesday": "середа", "Thursday": "четвер", "Friday": "п'ятниця", "Saturday": "субота", "Sunday": "неділя"}
AI_TO_DB_WEEKS = {"odd": "непарна", "even": "парна", "both": "кожна"}

PAIR_TIMES = {
    1: "08:00",
    2: "09:50",
    3: "11:40",
    4: "13:30",
    5: "15:20"
}

flask_app = None
application = None
main_loop = None
scheduler = APScheduler()
ai_client = cohere.AsyncClient(COHERE_API_KEY) if COHERE_API_KEY else None

# ==========================================
# БАЗА ДАНИХ ТА ІСТОРІЯ ФАКТІВ
# ==========================================
def get_db_conn():
    return psycopg2.connect(DATABASE_URL, sslmode='disable', cursor_factory=psycopg2.extras.DictCursor)

def init_db():
    if not DATABASE_URL: return
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute('''CREATE TABLE IF NOT EXISTS schedule
                                 (id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, day TEXT NOT NULL,
                                  time TEXT NOT NULL, name TEXT NOT NULL, link TEXT,
                                  week_type TEXT NOT NULL DEFAULT 'кожна', pair_order INTEGER DEFAULT 0)''')
                cursor.execute('''CREATE TABLE IF NOT EXISTS users
                                 (user_id BIGINT PRIMARY KEY, username TEXT, subscribed INTEGER DEFAULT 1)''')
                cursor.execute('''CREATE TABLE IF NOT EXISTS sent_notifications
                                 (notification_key TEXT PRIMARY KEY, sent_at TIMESTAMP WITH TIME ZONE NOT NULL)''')
                cursor.execute('''CREATE TABLE IF NOT EXISTS user_facts
                                 (id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, fact_summary TEXT NOT NULL,
                                  created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP)''')
                cursor.execute('''CREATE TABLE IF NOT EXISTS deleted_pairs
                                 (id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, day TEXT NOT NULL,
                                  time TEXT NOT NULL, name TEXT NOT NULL, link TEXT,
                                  week_type TEXT NOT NULL DEFAULT 'кожна', pair_order INTEGER DEFAULT 0,
                                  deleted_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP)''')
            conn.commit()
    except Exception as e:
        print(f"ПОМИЛКА init_db: {e}")

# ==========================================
# ФУНКЦІЇ ДЛЯ ВІДНОВЛЕННЯ ВИДАЛЕНИХ ПАР
# ==========================================
def save_deleted_pairs(user_id: int, pairs_to_save: list):
    """Зберігає пари в таблицю deleted_pairs перед видаленням."""
    if not pairs_to_save:
        return
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cursor:
                for p in pairs_to_save:
                    cursor.execute(
                        "INSERT INTO deleted_pairs (user_id, day, time, name, link, week_type, pair_order) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (user_id, p['day'], p['time'], p['name'], str(p['link']), p['week_type'], p['pair_order'])
                    )
                # Залишаємо тільки останні 20 видалених
                cursor.execute(
                    "DELETE FROM deleted_pairs WHERE id NOT IN (SELECT id FROM deleted_pairs WHERE user_id=%s ORDER BY deleted_at DESC LIMIT 20)",
                    (user_id,)
                )
            conn.commit()
    except Exception as e:
        print(f"Помилка save_deleted_pairs: {e}")

def get_last_deleted_pairs(user_id: int) -> list:
    """Повертає останні видалені пари."""
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cursor:
                # Спочатку шукаємо за останні 60 секунд
                cursor.execute(
                    "SELECT day, time, name, link, week_type, pair_order FROM deleted_pairs WHERE user_id=%s AND deleted_at >= NOW() - INTERVAL '60 seconds' ORDER BY deleted_at DESC LIMIT 10",
                    (user_id,)
                )
                rows = cursor.fetchall()
                if not rows:
                    # Якщо нічого свіжого — беремо найостаннішу групу
                    cursor.execute(
                        "SELECT day, time, name, link, week_type, pair_order FROM deleted_pairs WHERE user_id=%s ORDER BY deleted_at DESC LIMIT 5",
                        (user_id,)
                    )
                    rows = cursor.fetchall()
                return [dict(r) for r in rows]
    except Exception:
        return []

def format_deleted_pairs_for_prompt(pairs: list) -> str:
    if not pairs:
        return "Немає нещодавно видалених пар."
    lines = []
    for p in pairs:
        lines.append(f"- {p['day'].capitalize()}, пара {p['pair_order']}, {p['time']}, {p['name']}, тиждень: {p['week_type']}, link: {p['link']}")
    return "\n".join(lines)


# Функції для роботи з фактами
def get_recent_facts(user_id: int):
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT fact_summary FROM user_facts WHERE user_id = %s ORDER BY id DESC LIMIT 15", (user_id,))
                return [row[0] for row in cursor.fetchall()]
    except Exception:
        return []

def save_fact(user_id: int, fact_summary: str):
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute("INSERT INTO user_facts (user_id, fact_summary) VALUES (%s, %s)", (user_id, fact_summary[:100]))
            conn.commit()
    except Exception as e:
        print(f"Помилка збереження факту: {e}")

async def generate_unique_fact(user_id: int) -> str:
    if not ai_client: return "Cohere API не підключено."

    history = get_recent_facts(user_id)
    history_str = "\n".join(f"- {f}" for f in history) if history else "Немає історії."

    prompt = f"""Розкажи ОДИН дуже цікавий, маловідомий факт про архітектуру ПК, мережі, кібербезпеку або програмування.
    Пиши суто текст, без форматування і без довгих вступів.
    ЗАБОРОНЕНІ ФАКТИ (користувач їх вже знає):
    {history_str}"""

    try:
        response = await ai_client.chat(
            message="Сгенеруй цікавий ІТ-факт.",
            preamble=prompt,
            model="command-a-03-2025",
            temperature=0.7
        )
        fact = response.text.strip()
        save_fact(user_id, fact)
        return fact
    except Exception as e:
        return "Не вдалося згенерувати факт."

# ==========================================
# ФУНКЦІЇ РОБОТИ З БД
# ==========================================
def add_pair_to_db(user_id: int, day: str, time_str: str, name: str, link: str, week_type: str, pair_order: int = 0):
    sql = "INSERT INTO schedule (user_id, day, time, name, link, week_type, pair_order) VALUES (%s, %s, %s, %s, %s, %s, %s)"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (user_id, day.lower(), time_str, name, link, week_type, pair_order))
        conn.commit()

def delete_specific_pair(user_id: int, day: str, pair_order: int, week_type: str):
    # Спочатку зберігаємо пари що будуть видалені
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cursor:
                if week_type == "кожна":
                    cursor.execute("SELECT * FROM schedule WHERE user_id=%s AND day=%s AND pair_order=%s", (user_id, day.lower(), pair_order))
                else:
                    cursor.execute("SELECT * FROM schedule WHERE user_id=%s AND day=%s AND pair_order=%s AND week_type IN (%s, 'кожна')", (user_id, day.lower(), pair_order, week_type))
                to_save = [dict(r) for r in cursor.fetchall()]
        save_deleted_pairs(user_id, to_save)
    except Exception:
        pass

    if week_type == "кожна":
        sql = "DELETE FROM schedule WHERE user_id=%s AND day=%s AND pair_order=%s"
        params = (user_id, day.lower(), pair_order)
    else:
        sql = "DELETE FROM schedule WHERE user_id=%s AND day=%s AND pair_order=%s AND week_type IN (%s, 'кожна')"
        params = (user_id, day.lower(), pair_order, week_type)

    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
        conn.commit()

# ==========================================
# НОВА ФУНКЦІЯ: видалення за ключовими словами у назві (підтримує список)
# ==========================================
def delete_pair_by_name(user_id: int, day: str, name_keywords) -> int:
    """
    Видаляє всі пари у вказаний день, назва яких містить будь-яке з name_keywords.
    name_keywords може бути рядком або списком рядків.
    Повертає кількість видалених рядків.
    """
    if isinstance(name_keywords, str):
        name_keywords = [name_keywords]

    # Будуємо OR-умову для кожного ключового слова
    conditions = " OR ".join(["LOWER(name) LIKE %s" for _ in name_keywords])
    params = [user_id, day.lower()] + [f"%{kw.lower()}%" for kw in name_keywords]

    # Спочатку зберігаємо пари що будуть видалені
    try:
        save_params = [user_id, day.lower()] + [f"%{kw.lower()}%" for kw in name_keywords]
        save_conditions = " OR ".join(["LOWER(name) LIKE %s" for _ in name_keywords])
        with get_db_conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute(f"SELECT * FROM schedule WHERE user_id=%s AND day=%s AND ({save_conditions})", save_params)
                to_save = [dict(r) for r in cursor.fetchall()]
        save_deleted_pairs(user_id, to_save)
    except Exception:
        pass

    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"DELETE FROM schedule WHERE user_id=%s AND day=%s AND ({conditions})",
                params
            )
            deleted = cursor.rowcount
        conn.commit()
    return deleted

def execute_db_actions(user_id: int, actions_list):
    processed_count = 0
    if not isinstance(actions_list, list): return 0

    for item in actions_list:
        action = item.get("action")

        if action == "DELETE_ALL":
            with get_db_conn() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("DELETE FROM schedule WHERE user_id=%s", (user_id,))
                conn.commit()
            processed_count += 1
            continue

        # ---- ДІЯ: видалення за назвою (підтримує список keywords) ----
        if action == "DELETE_BY_NAME":
            data = item.get("data", {})
            day_eng = data.get("day")
            # Підтримуємо і старе поле name_keyword, і нове name_keywords (список)
            name_keywords = data.get("name_keywords") or data.get("name_keyword", "")
            if isinstance(name_keywords, str):
                name_keywords = [kw.strip() for kw in name_keywords.split(",") if kw.strip()]
            if day_eng and name_keywords:
                day_ukr = AI_TO_DB_DAYS.get(day_eng, "")
                if day_ukr:
                    deleted = delete_pair_by_name(user_id, day_ukr, name_keywords)
                    processed_count += deleted
            continue
        # -------------------------------------------------------

        # ---- ДІЯ: відновлення останніх видалених пар ----
        if action == "RESTORE":
            pairs_to_restore = get_last_deleted_pairs(user_id)
            for p in pairs_to_restore:
                try:
                    # Спочатку видаляємо дублікат якщо вже існує (щоб не було 5 пар)
                    delete_specific_pair(user_id, p['day'], p['pair_order'], p['week_type'])
                    add_pair_to_db(user_id, p['day'], p['time'], p['name'], str(p['link']), p['week_type'], p['pair_order'])
                    processed_count += 1
                except Exception as e:
                    print(f"Помилка відновлення пари: {e}")
            continue
        # -------------------------------------------------

        # ---- ДІЯ: поміняти дві пари місцями ----
        if action == "SWAP":
            data = item.get("data", {})
            day_eng = data.get("day")
            order_a = data.get("order_a")
            order_b = data.get("order_b")
            week_eng = data.get("week", "both")
            if day_eng and order_a and order_b:
                day_ukr = AI_TO_DB_DAYS.get(day_eng, "")
                week_ukr = AI_TO_DB_WEEKS.get(week_eng, "кожна")
                if day_ukr:
                    try:
                        with get_db_conn() as conn:
                            with conn.cursor() as cursor:
                                # Читаємо обидві пари
                                cursor.execute("SELECT * FROM schedule WHERE user_id=%s AND day=%s AND pair_order=%s AND (week_type=%s OR week_type='кожна')", (user_id, day_ukr, order_a, week_ukr))
                                pairs_a = [dict(r) for r in cursor.fetchall()]
                                cursor.execute("SELECT * FROM schedule WHERE user_id=%s AND day=%s AND pair_order=%s AND (week_type=%s OR week_type='кожна')", (user_id, day_ukr, order_b, week_ukr))
                                pairs_b = [dict(r) for r in cursor.fetchall()]
                                # Видаляємо обидві
                                cursor.execute("DELETE FROM schedule WHERE user_id=%s AND day=%s AND pair_order IN (%s,%s)", (user_id, day_ukr, order_a, order_b))
                            conn.commit()
                        # Вставляємо з переставленими номерами та часом
                        time_a = PAIR_TIMES.get(order_a, "00:00")
                        time_b = PAIR_TIMES.get(order_b, "00:00")
                        for p in pairs_a:
                            add_pair_to_db(user_id, day_ukr, time_b, p['name'], str(p['link']), p['week_type'], order_b)
                        for p in pairs_b:
                            add_pair_to_db(user_id, day_ukr, time_a, p['name'], str(p['link']), p['week_type'], order_a)
                        processed_count += 1
                    except Exception as e:
                        print(f"Помилка SWAP: {e}")
            continue
        # ------------------------------------------

        data = item.get("data", {})
        if not action or not data: continue

        day_eng = data.get("day")
        week_eng = data.get("week", "both")
        order = data.get("order")
        subject = data.get("subject", "Без назви")
        link = data.get("link", "None")
        custom_time = data.get("custom_time")

        if not day_eng or not order: continue

        day_ukr = AI_TO_DB_DAYS.get(day_eng, "понеділок")
        week_ukr = AI_TO_DB_WEEKS.get(week_eng, "кожна")

        if custom_time:
            pair_time = custom_time
        else:
            pair_time = PAIR_TIMES.get(order, "00:00")

        if link is None: link = "None"

        if action in ["UPDATE", "ADD"]:
            if subject == "Без назви": continue
            delete_specific_pair(user_id, day_ukr, order, week_ukr)
            add_pair_to_db(user_id, day_ukr, pair_time, subject, link, week_ukr, order)
            processed_count += 1

        elif action == "DELETE":
            delete_specific_pair(user_id, day_ukr, order, week_ukr)
            processed_count += 1

    return processed_count

def get_pairs_for_day(user_id: int, day_to_fetch: str, week_type: str):
    sql = """SELECT id, user_id, %s AS day, time, name, link, week_type, pair_order
             FROM schedule WHERE user_id=%s AND day=%s AND (week_type='кожна' OR week_type=%s)
             ORDER BY time::TIME ASC"""
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (day_to_fetch.lower(), user_id, day_to_fetch.lower(), week_type))
            return cursor.fetchall()

def get_all_pairs(user_id: int):
    sql_cases = [f"WHEN day = '{day.replace(chr(39), chr(39)*2)}' THEN {i}" for i, day in enumerate(DAY_ORDER_LIST)]
    day_order_sql_case = " ".join(sql_cases)
    sql = f"SELECT *, CASE {day_order_sql_case} ELSE 99 END as day_order FROM schedule WHERE user_id=%s ORDER BY week_type, day_order, time::TIME ASC"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (user_id,))
            return cursor.fetchall()

def get_pairs_for_day_forced_week(user_id: int, day_name: str, forced_week: str):
    """forced_week: 'парна' | 'непарна' | None (auto from current date)"""
    if forced_week is None:
        # Use real current week type for that day
        now = datetime.now(TIMEZONE)
        current_monday = now.date() - timedelta(days=now.weekday())
        DAY_NAME_TO_OFFSET_LOCAL = {"понеділок": 0, "вівторок": 1, "середа": 2, "четвер": 3, "п'ятниця": 4}
        offset = DAY_NAME_TO_OFFSET_LOCAL.get(day_name, 0)
        target_date = current_monday + timedelta(days=offset)
        forced_week = get_week_type_for_date(target_date)
    return get_pairs_for_day(user_id, day_name, forced_week)

def get_schedule_for_specific_week(user_id: int, forced_week: str):
    """Get full week schedule forcing a specific week type (парна/непарна)."""
    all_week_pairs = []
    for i in range(5):
        day_name = DAY_ORDER_LIST[i]
        day_pairs = get_pairs_for_day(user_id, day_name, forced_week)
        all_week_pairs.extend(day_pairs)
    return all_week_pairs

def get_schedule_for_current_week(user_id: int, start_of_week_date):
    all_week_pairs = []
    for i in range(5):
        current_day_date = start_of_week_date + timedelta(days=i)
        current_day_name = DAY_OF_WEEK_UKR[i]
        current_week_type = get_week_type_for_date(current_day_date)
        day_pairs = get_pairs_for_day(user_id, current_day_name, current_week_type)
        all_week_pairs.extend(day_pairs)
    return all_week_pairs

def get_week_type_for_date(date_obj):
    days_diff = (date_obj - REFERENCE_DATE).days
    weeks_diff = days_diff // 7
    if weeks_diff % 2 == 0:
        return "парна" if REFERENCE_WEEK_TYPE == "парний" else "непарна"
    else:
        return "непарна" if REFERENCE_WEEK_TYPE == "парний" else "парна"

def get_current_week_type():
    return get_week_type_for_date(datetime.now(TIMEZONE).date())

def format_pairs_message(pairs, title):
    if not pairs: return f"{title}\n\n🎉 Пар немає!"
    message = f"{title}\n"
    current_week_type, current_day = "", ""
    show_ids = 'id' in title.lower() or 'управління' in title.lower()

    for pair in pairs:
        if show_ids and pair['week_type'] != current_week_type:
            current_week_type = pair['week_type']
            display_week = {"парна": "ПАРНИЙ", "непарна": "НЕПАРНИЙ", "кожна": "КОЖЕН"}.get(current_week_type, current_week_type.upper())
            message += f"\n--- **{display_week} ТИЖДЕНЬ** ---\n"
            current_day = ""

        if pair['day'] != current_day:
            current_day = pair['day']
            message += f"\n**{current_day.capitalize()}**\n"

        link_str = str(pair['link']).strip()
        link_info = ""
        if link_str and link_str.lower() != 'none':
            if link_str.startswith("http"):
                link_info = f"\n     🔗 [Відкрити посилання]({link_str})"
            else:
                link_info = f"\n     ℹ️ Дані підключення: `{link_str}`"

        order_display = pair.get('pair_order', '?')
        if order_display == 99: order_display = "Тест"

        message += f"  Пара {order_display}) `{pair['time']}` - {pair['name']}{link_info}\n"
        if show_ids: message += f"     *(ID: `{pair['id']}`)*\n"
    return message

# ==========================================
# ФУНКЦІЇ ДЛЯ НАГАДУВАНЬ (CRON)
# ==========================================
def get_all_subscribed_users():
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT user_id FROM users WHERE subscribed = 1")
            return [row[0] for row in cursor.fetchall()]

def check_if_notified(notification_key: str):
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1 FROM sent_notifications WHERE notification_key = %s", (notification_key,))
            return cursor.fetchone() is not None

def mark_as_notified(notification_key: str):
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO sent_notifications (notification_key, sent_at) VALUES (%s, %s)", (notification_key, datetime.now(TIMEZONE)))
        conn.commit()

def cleanup_old_notifications():
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM sent_notifications WHERE sent_at < %s", (datetime.now(TIMEZONE) - timedelta(days=2),))
            conn.commit()
    except Exception:
        pass

async def check_and_send_reminders(bot: Bot):
    try:
        now = datetime.now(TIMEZONE)
        weekday = now.weekday()
        if weekday >= 5: return

        target_time_obj = (now + timedelta(minutes=REMIND_BEFORE_MINUTES)).time().replace(second=0, microsecond=0)
        current_day_name = DAY_OF_WEEK_UKR[weekday]
        week_type_to_check = get_current_week_type()

        subscribed_users = get_all_subscribed_users()
        if not subscribed_users: return

        pairs_today = get_pairs_for_day(ADMIN_ID, current_day_name, week_type_to_check)

        for user_id in subscribed_users:
            for pair in pairs_today:
                try:
                    if datetime.strptime(pair['time'], '%H:%M').time() == target_time_obj:
                        notification_key = f"{user_id}_{pair['id']}_{now.strftime('%Y-%m-%d')}"
                        if not check_if_notified(notification_key):
                            link_str = str(pair['link']).strip()
                            link_msg = ""
                            if link_str and link_str.lower() != 'none':
                                if link_str.startswith("http"):
                                    link_msg = f"\n\n🔗 [Відкрити пару]({link_str})"
                                else:
                                    link_msg = f"\n\nℹ️ Дані підключення:\n`{link_str}`"

                            fact = await generate_unique_fact(user_id)
                            # Перше повідомлення — нагадування з посиланням
                            msg = f"🔔 **Нагадування!**\n\nЧерез {REMIND_BEFORE_MINUTES} хвилин ({pair['time']}) почнеться пара:\n**{pair['name']}**{link_msg}"
                            await bot.send_message(user_id, msg, parse_mode="Markdown", disable_web_page_preview=True)
                            # Друге повідомлення — окремо ІТ-факт
                            await bot.send_message(user_id, f"💡 **Цікавий ІТ-факт:**\n\n_{fact}_", parse_mode="Markdown")
                            mark_as_notified(notification_key)
                except Exception:
                    pass
        cleanup_old_notifications()
    except Exception as e:
        print(f"Помилка нагадувань: {e}")

def scheduled_job_wrapper():
    if application and application.bot and main_loop:
        asyncio.run_coroutine_threadsafe(check_and_send_reminders(application.bot), main_loop)

# ==========================================
# ТЕЛЕГРАМ ОБРОБНИКИ ТА ШІ
# ==========================================
async def ai_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS: return
    if not ai_client: return await update.message.reply_text("❌ Ключ Cohere не підключено.")

    text = update.message.text
    text_lower = text.lower()

    # ============================================================
    # ДОПОМІЖНА ФУНКЦІЯ для форматування дня як /today
    # ============================================================
    def format_day_like_today(target_date, label_prefix: str):
        weekday = target_date.weekday()
        if weekday >= 5:
            return f"{label_prefix}\n\n🎉 Вихідний!"
        day_name = DAY_OF_WEEK_UKR[weekday]
        week = get_week_type_for_date(target_date)
        week_label = "парний" if week == "парна" else "непарний"
        pairs = get_pairs_for_day(ADMIN_ID, day_name, week)
        title = f"🔵 {label_prefix} ({day_name.capitalize()}, {week_label} тиждень)"
        return format_pairs_message(pairs, title)

    # ============================================================
    # ШВИДКИЙ ПЕРЕХВАТ: ПОРЯДОК ВАЖЛИВИЙ — спочатку конкретні дні,
    # потім тиждень. Інакше "розклад на завтра" потрапляє у тижневий.
    #
    # ВАЖЛИВО: якщо є ключові слова дії (видали/додай/змін/відновити) —
    # пропускаємо перехват і йдемо до AI для мультизадачності.
    # ============================================================

    ACTION_KEYWORDS = [
        "видали", "видаліть", "удали", "удалить", "прибери", "прибрати",
        "додай", "добавь", "добавити", "додати",
        "зміни", "змін", "измени", "виправ", "виправити",
        "відновити", "восстанови", "восстановить", "поверни", "верни",
        "зроби", "зробити", "создай", "создать", "сделай", "сделать",
        "постав", "поміняй", "поменяй", "переставь", "перестав",
        "перепиши", "перезапиши", "запиши", "переписати", "перезаписати", "записати",
        "swap", "move",
    ]
    has_action = any(kw in text_lower for kw in ACTION_KEYWORDS)

    # ===========================================================
    # SMART INTERCEPTOR v3: segment-based, fixes парн/непарн bug
    # ===========================================================

    # ---- Визначення типу тижня (НЕПАРН* перед ПАРН* щоб уникнути substring-помилки) ----
    def detect_week_type(t: str):
        """Returns 'парна', 'непарна', or None. Checks непарн* FIRST."""
        odd_words = [
            "непарну", "непарний", "непарна", "непарної", "непарного",
            "непарной", "непарному", "непарный",
            "нечётну", "нечётний", "нечётна", "нечётной", "нечётный",
            "нечетну", "нечетний", "нечетна", "нечетной", "нечетную", "нечетный",
            "непарную", "odd",
            "непарне", "нечётне", "нечетное", "непарное",
        ]
        even_words = [
            "парну", "парний", "парна", "парної", "парного",
            "парной", "парному", "парный",
            "четну", "четний", "четна", "четной", "четную", "четный",
            "чётну", "чётній", "чётна", "чётной", "чётную", "чётный",
            "парную", "even",
            "парне", "парное", "четное", "чётное",
        ]
        # MUST check odd first — "парну" IS a substring of "непарну"
        for w in odd_words:
            if w in t:
                return "непарна"
        for w in even_words:
            if w in t:
                return "парна"
        return None

    # ---- Мапи днів ----
    DAY_DETECT_MAP = [
        (["понеділок", "понеділку", "в понеділок", "на понеділок",
          "понедельник", "в понедельник", "на понедельник", "понедельника"], "понеділок", 0),
        (["вівторок", "у вівторок", "на вівторок",
          "вторник", "во вторник", "на вторник", "вторника"], "вівторок", 1),
        (["середу", "середа", "середи", "в середу", "на середу",
          "среду", "в среду", "на среду", "среды"], "середа", 2),
        (["четвер", "в четвер", "на четвер", "четвер",
          "четверг", "в четверг", "на четверг", "четверга"], "четвер", 3),
        (["п'ятницю", "п'ятниця", "п'ятниці", "в п'ятницю", "на п'ятницю",
          "пятницю", "пятниця", "пятницу", "в пятницу", "на пятницу",
          "пятницы", "пятница"], "п'ятниця", 4),
    ]

    def detect_day(seg: str):
        """Returns (day_name, offset) or None."""
        for kws, day_name, offset in DAY_DETECT_MAP:
            if any(kw in seg for kw in kws):
                return (day_name, offset)
        return None

    # ---- Розбивка на сегменти ----
    CONJUNCTIONS = [" і ", " и ", " та ", " and ", " & ", ", "]

    def split_segments(t: str):
        """Split text into segments by conjunctions."""
        result = t
        for sep in CONJUNCTIONS:
            result = result.replace(sep, " ||SEP|| ")
        parts = [p.strip() for p in result.split("||SEP||") if p.strip()]
        return parts if parts else [t]

    # ---- Класифікація сегменту ----
    SHOW_DAY_TRIGGERS = [
        "розклад", "пари", "виведи", "покажи", "дай",
        "расписание", "пары", "выведи",
        "на ", "в ", "у ",
    ]
    WEEK_FULL_KW = [
        "тиждень", "тижня", "тижні", "неделю", "недели",
        "всі пари", "весь розклад", "повний розклад",
        "полное расписание", "все пары",
        "парне розклад", "непарне розклад",
        "парное расписание", "непарное расписание",
    ]
    TOMORROW_KW = [
        "завтра", "завтрашн",
    ]
    TODAY_KW = [
        "сьогодні", "сегодня",
    ]
    FACT_KW = [
        "факт", "факти", "fact",
    ]

    def classify_segment(seg: str):
        """
        Returns one of: 'today', 'tomorrow', 'week', 'day', 'fact', 'ai'
        """
        s = seg.lower()
        if any(kw in s for kw in FACT_KW):
            return "fact"
        if any(kw in s for kw in TOMORROW_KW):
            return "tomorrow"
        if any(kw in s for kw in TODAY_KW):
            return "today"
        if any(kw in s for kw in WEEK_FULL_KW):
            return "week"
        # Check for day name
        if detect_day(s) is not None:
            # Require a show trigger OR short segment
            if any(t in s for t in SHOW_DAY_TRIGGERS) or len(s.split()) <= 4:
                return "day"
        return "ai"

    # ---- Функція формування повідомлення для дня ----
    def make_day_msg(day_name: str, offset: int, wtype):
        now_dt = datetime.now(TIMEZONE)
        current_monday = now_dt.date() - timedelta(days=now_dt.weekday())
        target_date = current_monday + timedelta(days=offset)
        if wtype:
            pairs = get_pairs_for_day(ADMIN_ID, day_name, wtype)
            wlabel = "парний" if wtype == "парна" else "непарний"
            title = f"🗓️ {day_name.capitalize()} ({wlabel} тиждень)"
            return format_pairs_message(pairs, title)
        else:
            return format_day_like_today(target_date, day_name.capitalize())

    # ---- Основний розбір ----
    if not has_action:
        segments = split_segments(text_lower)
        ai_segments = []
        schedule_tasks = []  # list of async callables
        has_fact_request = False

        for seg in segments:
            kind = classify_segment(seg)

            if kind == "fact":
                has_fact_request = True

            elif kind == "tomorrow":
                wtype = detect_week_type(seg)
                async def _send_tomorrow(wt=wtype):
                    now_dt = datetime.now(TIMEZONE)
                    tomorrow_dt = (now_dt + timedelta(days=1)).date()
                    if wt:
                        dn = DAY_OF_WEEK_UKR.get(tomorrow_dt.weekday(), "п'ятниця")
                        pairs = get_pairs_for_day(ADMIN_ID, dn, wt)
                        wlabel = "парний" if wt == "парна" else "непарний"
                        msg = format_pairs_message(pairs, f"🔵 Завтра ({dn.capitalize()}, {wlabel} тиждень)")
                    else:
                        msg = format_day_like_today(tomorrow_dt, "Завтра")
                    await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)
                schedule_tasks.append(_send_tomorrow)

            elif kind == "today":
                wtype = detect_week_type(seg)
                async def _send_today(wt=wtype):
                    now_dt = datetime.now(TIMEZONE)
                    today_dt = now_dt.date()
                    if wt:
                        dn = DAY_OF_WEEK_UKR.get(today_dt.weekday(), "понеділок")
                        pairs = get_pairs_for_day(ADMIN_ID, dn, wt)
                        wlabel = "парний" if wt == "парна" else "непарний"
                        msg = format_pairs_message(pairs, f"🔵 Сьогодні ({dn.capitalize()}, {wlabel} тиждень)")
                    else:
                        msg = format_day_like_today(today_dt, "Сьогодні")
                    await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)
                schedule_tasks.append(_send_today)

            elif kind == "week":
                wtype = detect_week_type(seg)
                async def _send_week(wt=wtype):
                    now_dt = datetime.now(TIMEZONE)
                    if wt:
                        pairs = get_schedule_for_specific_week(ADMIN_ID, wt)
                        wlabel = "ПАРНИЙ" if wt == "парна" else "НЕПАРНИЙ"
                        msg = format_pairs_message(pairs, f"🗓️ Розклад на **{wlabel}** тиждень")
                    else:
                        cw = "парний" if get_current_week_type() == "парна" else "непарний"
                        pairs = get_schedule_for_current_week(ADMIN_ID, now_dt.date() - timedelta(days=now_dt.weekday()))
                        msg = format_pairs_message(pairs, f"🗓️ Розклад на **{cw.upper()}** тиждень")
                    await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)
                schedule_tasks.append(_send_week)

            elif kind == "day":
                day_result = detect_day(seg)
                if day_result:
                    day_name, offset = day_result
                    wtype = detect_week_type(seg)
                    async def _send_day(dn=day_name, off=offset, wt=wtype):
                        msg = make_day_msg(dn, off, wt)
                        await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)
                    schedule_tasks.append(_send_day)

            else:
                ai_segments.append(seg)

        # Виконуємо всі schedule tasks
        for task in schedule_tasks:
            await task()

        # Якщо є запит на факт — генеруємо і надсилаємо
        if has_fact_request:
            fact = await generate_unique_fact(user_id)
            await update.message.reply_text(
                "🎲 **Цікавий ІТ-факт:**\n\n" + fact,
                parse_mode="Markdown"
            )

        # Якщо є завдання для AI — продовжуємо (не робимо return)
        # Якщо немає — зупиняємось
        if not ai_segments and (schedule_tasks or has_fact_request):
            return

        # Якщо є AI-сегменти — збираємо їх і відправляємо до AI
        if ai_segments:
            # Replace text with only AI-relevant parts for the AI call below
            text = " і ".join(ai_segments)
            text_lower = text.lower()
        elif not schedule_tasks and not has_fact_request:
            # No segments matched anything — also check bare "виведи розклад" fallback
            pass
    # ============================================================

    # ============================================================

    # Fallback: bare "виведи розклад" / "покажи розклад" without specific day/week
    if not has_action:
        bare_show_kw = [
            "виведи розклад", "покажи розклад", "дай розклад",
            "выведи расписание", "покажи расписание", "дай расписание",
        ]
        if any(kw in text_lower for kw in bare_show_kw):
            now_dt = datetime.now(TIMEZONE)
            cw = "парний" if get_current_week_type() == "парна" else "непарний"
            pairs = get_schedule_for_current_week(ADMIN_ID, now_dt.date() - timedelta(days=now_dt.weekday()))
            msg = format_pairs_message(pairs, f"🗓️ Розклад на **{cw.upper()}** тиждень")
            return await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)

    now = datetime.now(TIMEZONE)
    current_time_str = now.strftime('%Y-%m-%d %H:%M:%S')

    current_day_name = DAY_OF_WEEK_UKR.get(now.weekday(), "невідомо")
    current_week_str = get_current_week_type()
    if now.weekday() < 5:
        pairs_today = get_pairs_for_day(ADMIN_ID, current_day_name, current_week_str)
        text_today = format_pairs_message(pairs_today, f"Сьогодні ({current_day_name}, {current_week_str} тиждень):")
    else:
        text_today = "Сьогодні вихідний, пар немає!"

    tomorrow = now + timedelta(days=1)
    tomorrow_day_name = DAY_OF_WEEK_UKR.get(tomorrow.weekday(), "невідомо")
    tomorrow_week_str = get_week_type_for_date(tomorrow.date())
    if tomorrow.weekday() < 5:
        pairs_tomorrow = get_pairs_for_day(ADMIN_ID, tomorrow_day_name, tomorrow_week_str)
        text_tomorrow = format_pairs_message(pairs_tomorrow, f"Завтра ({tomorrow_day_name}, {tomorrow_week_str} тиждень):")
    else:
        text_tomorrow = "Завтра вихідний, пар немає!"

    all_pairs = get_all_pairs(ADMIN_ID)
    text_all = format_pairs_message(all_pairs, "Повний розклад (всі записи):")

    # Останні видалені пари (для контексту відновлення)
    last_deleted = get_last_deleted_pairs(ADMIN_ID)
    text_deleted = format_deleted_pairs_for_prompt(last_deleted)

    system_prompt = f"""
    Ти — розумний персональний асистент Олега з розкладу (Одеська політехніка).
    МОВА ВІДПОВІДІ: ВИКЛЮЧНО УКРАЇНСЬКА. Ніякої російської, англійської чи суржику.

    --- ПОТОЧНІ ДАНІ ---
    ЧАС: {current_time_str}

    [РОЗКЛАД НА СЬОГОДНІ]:
    {text_today}

    [РОЗКЛАД НА ЗАВТРА]:
    {text_tomorrow}

    [ПОВНИЙ РОЗКЛАД В БД]:
    {text_all}

    [ОСТАННІ ВИДАЛЕНІ ПАРИ (для відновлення)]:
    {text_deleted}
    ---------------------------------------------------

    ПОВЕРТАЙ ВИКЛЮЧНО ВАЛІДНИЙ JSON (без зайвого тексту, без markdown-блоків):
    {{
      "reply": "Відповідь ТІЛЬКИ УКРАЇНСЬКОЮ...",
      "give_fact": false,
      "show_schedule": null,
      "db_actions": []
    }}

    ============================================================
    ПРАВИЛА СПІЛКУВАННЯ:
    ============================================================
    1. Привітання ("Привіт", "Доброго дня") → відповідай у "reply", db_actions порожній.
    2. Загальні питання НЕ по розкладу ("що таке бекенд", "погода") →
       reply: "Я можу лише керувати розкладом та видавати ІТ-факти."
    3. Факти ("цікавий факт", "дай факт", "рандомний факт", "random fact") → "give_fact": true, reply: ""
       ВАЖЛИВО: якщо give_fact=true — поле "reply" ЗАВЖДИ порожній рядок "". Факт буде доданий автоматично.
    4. Питання про розклад ("яка перша пара", "що є в середу") → відповідай з даних вище.

    ============================================================
    МУЛЬТИЗАДАЧНІСТЬ (show_schedule):
    ============================================================
    Якщо в запиті є дія З БД (видалити/додати/змінити) І запит на показ розкладу —
    виконай дію І встанови поле "show_schedule":
    - "today"          → показати розклад на сьогодні
    - "tomorrow"       → показати розклад на завтра
    - "week"           → показати розклад на поточний тиждень
    - "week_even"      → показати розклад на ПАРНИЙ тиждень
    - "week_odd"       → показати розклад на НЕПАРНИЙ тиждень
    - "day:понеділок"  → конкретний день (підстав потрібну назву укр.)

    Приклади:
    "видали англійську завтра і покажи розклад на завтра" →
      db_actions: [DELETE_BY_NAME Friday], show_schedule: "tomorrow"
    "додай пару і виведи всі пари на тиждень" →
      db_actions: [ADD ...], show_schedule: "week"
    "видали матан у вівторок, покажи вівторок" →
      db_actions: [DELETE_BY_NAME Tuesday], show_schedule: "day:вівторок"
    "перепиши розклад і покажи парний тиждень" →
      db_actions: [DELETE_ALL, ADD...], show_schedule: "week_even"
    Якщо показ не потрібен — show_schedule: null

    ============================================================
    ПРАВИЛА МАСОВОГО ОНОВЛЕННЯ РОЗКЛАДУ:
    ============================================================
    Якщо повідомлення містить ПОВНИЙ РОЗКЛАД (є заголовки днів ПОНЕДІЛОК/ВІВТОРОК/СЕРЕДА/ЧЕТВЕР/П'ЯТНИЦЯ і списки пар):

    КРОК 1 — ПЕРША дія ЗАВЖДИ:
    {{"action": "DELETE_ALL"}}

    КРОК 2 — Для кожної пари генеруй окрему дію "ADD".
    Пропускай пари де написано "пусто" або немає назви.

    КРОК 3 — ВИЗНАЧЕННЯ ТИЖНЯ (КРИТИЧНО ВАЖЛИВО):
    Формат у розкладі: "Якщо X-Y тип, то назва"
    де X-Y = номери тижнів семестру (1-15).

    ПРАВИЛО:
    - Якщо діапазон починається з ПАРНОГО числа (2, 4...) → тобто "2-14" → week: "even"
    - Якщо діапазон починається з НЕПАРНОГО числа (1, 3...) → тобто "1-15" або "3-15" → week: "odd"
    - Якщо NO умови АБО "1-15" без слова "Якщо" → week: "both"

    ЯКЩО ДЛЯ ОДНІЄЇ ПАРИ Є ДВІ УМОВИ (один предмет парний + інший непарний) —
    ОБОВ'ЯЗКОВО генеруй ДВІ окремі дії ADD з різними "week".

    ПРИКЛАД (саме такий формат зустрінеться):
    "3 пара:
     Якщо 2-14 лабораторна, то Політики кібербезпеки — Мельник
     Якщо 3-15 лабораторна, то Спеціальні розділи фізики — Дедюра"
    
    Правильний результат — ДВІ дії:
    {{"action":"ADD","data":{{"day":"Friday","order":3,"week":"even","subject":"Політики кібербезпеки та захисту інформації - доц. Мельник Г.М.","link":"https://..."}}}},
    {{"action":"ADD","data":{{"day":"Friday","order":3,"week":"odd","subject":"Спеціальні розділи фізики - Дедюра К.О.","link":"https://..."}}}}

    КРОК 4 — "subject": бери назву ТОЧНО як написано. Якщо назва є в словнику нижче — використовуй повну офіційну.
    КРОК 5 — "link": якщо після пари є URL або "№:..., Код доступу:..." — записуй ВСЕ в одному рядку.

    [СЛОВНИК ПРЕДМЕТІВ]:
    - "матан", "математика" → "Математичні основи захисту інформації - доц. Морозов Юрій Олександрович"
    - "прога", "програмування", "Ярова" → "Технологія програмування - доц. Ярова І.А."
    - "Головачова" → "Технології програмування - С.в. Головачова Олена Вікторівна"
    - "інф.технол", "інформатика", "Вінковська" → "Інформаційні технології - С.в. Вінковська Ірина Сергіївна"
    - "фізика", "Дедюра", "Спец. розділи" → "Спеціальні розділи фізики - Дедюра К.О."
    - "англ", "іноземна", "Єршова" → "Іноземна мова - Єршова Юлія Анатоліївна"
    - "англ", "іноземна", "Воробйова" → "Іноземна мова - Воробйова К.В."
    - "кібербезпека", "політики", "Мельник" → "Політики кібербезпеки та захисту інформації - доц. Мельник Г.М."
    - "філософія", "Афанасьєв" → "Філософія - Афанасьєв О.І."

    ============================================================
    ПРАВИЛА ТОЧКОВИХ ЗМІН:
    ============================================================
    - "зміни", "виправ", "додай" → дія "UPDATE"
    - Нестандартний час: "order": 99, "custom_time": "ЧЧ:ММ"
    - Дні: "Monday"–"Friday". Тижні: "odd"=непарна, "even"=парна, "both"=кожна

    ============================================================
    ПРАВИЛА ВИДАЛЕННЯ:
    ============================================================
    - Видалення за назвою → "DELETE_BY_NAME" з УСІМА аліасами предмету:
      {{"action":"DELETE_BY_NAME","data":{{"day":"Friday","name_keywords":["Іноземна мова","англійська","англ","Єршова","Воробйова"]}}}}
    - "DELETE" за номером → лише якщо явно вказано НОМЕР пари

    ============================================================
    ПРАВИЛА ПЕРЕСТАНОВКИ ПАР (SWAP):
    ============================================================
    Якщо Олег каже "поміняй місцями", "переставь", "swap", "поменяй местами" —
    генеруй дію SWAP:
      {{"action": "SWAP", "data": {{"day": "Friday", "order_a": 1, "order_b": 3, "week": "both"}}}}
    order_a і order_b — номери пар які треба поміняти місцями.
    НЕ використовуй DELETE + ADD для перестановки — тільки SWAP.

    ============================================================
    ПРАВИЛА ВІДНОВЛЕННЯ ВИДАЛЕНИХ ПАР:
    ============================================================
    Якщо Олег каже "відновити", "поверни", "восстанови", "поверни пару", "відміни видалення",
    "поверни першу пару", "поверни 1 пару", "верни пару", "верни расписание" —
    генеруй дію RESTORE:
      {{"action": "RESTORE"}}
    Система автоматично відновить останні видалені пари з [ОСТАННІ ВИДАЛЕНІ ПАРИ].
    У "reply" напиши які саме пари відновлено (з [ОСТАННІ ВИДАЛЕНІ ПАРИ]).
    ВАЖЛИВО: завжди використовуй RESTORE (НЕ ADD і НЕ UPDATE) — навіть якщо користувач
    вказав номер або назву пари. Відновлення відбувається автоматично з таблиці видалених.
    """

    processing_msg = await update.message.reply_text("⏳ Оброблюю запит...")

    try:
        response = await ai_client.chat(
            message=text,
            preamble=system_prompt,
            model="command-a-03-2025",
            temperature=0.1,
            max_tokens=8000
        )

        raw_text = response.text.strip()
        match = re.search(r'\{.*\}', raw_text, re.DOTALL)
        if match:
            clean_json = match.group(0)
        else:
            clean_json = raw_text.replace("```json", "").replace("```", "").strip()

        ai_json = json.loads(clean_json)

        reply_text = ai_json.get("reply", "")
        give_fact = ai_json.get("give_fact", False)
        db_actions = ai_json.get("db_actions", [])
        show_schedule = ai_json.get("show_schedule", None)  # "today" | "tomorrow" | "week" | "day:назва_дня"

        # Виконуємо всі дії з БД ПЕРШИМИ
        changes_count = execute_db_actions(ADMIN_ID, db_actions)

        final_message = reply_text
        if changes_count > 0:
            final_message += f"\n\n⚙️ _Виконано дій з базою: {changes_count}_"

        if give_fact:
            fact = await generate_unique_fact(user_id)
            fact_block = f"🎲 **Цікавий ІТ-факт:**\n\n{fact}"
            if final_message.strip():
                final_message += f"\n\n{fact_block}"
            else:
                final_message = fact_block

        await processing_msg.edit_text(final_message, parse_mode="Markdown", disable_web_page_preview=True)

        # Мультизадачність: якщо AI вказав show_schedule — відправляємо розклад ОКРЕМИМ повідомленням
        if show_schedule:
            now_dt = datetime.now(TIMEZONE)
            sched_msg = None
            if show_schedule == "today":
                sched_msg = format_day_like_today(now_dt.date(), "Сьогодні")
            elif show_schedule == "tomorrow":
                sched_msg = format_day_like_today((now_dt + timedelta(days=1)).date(), "Завтра")
            elif show_schedule == "week":
                current_week = "парний" if get_current_week_type() == "парна" else "непарний"
                sched_msg = format_pairs_message(
                    get_schedule_for_current_week(ADMIN_ID, now_dt.date() - timedelta(days=now_dt.weekday())),
                    f"🗓️ Розклад на **{current_week.upper()}** тиждень"
                )
            elif show_schedule == "week_even":
                sched_msg = format_pairs_message(
                    get_schedule_for_specific_week(ADMIN_ID, "парна"),
                    "🗓️ Розклад на **ПАРНИЙ** тиждень"
                )
            elif show_schedule == "week_odd":
                sched_msg = format_pairs_message(
                    get_schedule_for_specific_week(ADMIN_ID, "непарна"),
                    "🗓️ Розклад на **НЕПАРНИЙ** тиждень"
                )
            elif isinstance(show_schedule, str) and show_schedule.startswith("day:"):
                day_name = show_schedule[4:]
                DAY_NAME_TO_OFFSET = {"понеділок": 0, "вівторок": 1, "середа": 2, "четвер": 3, "п'ятниця": 4}
                offset = DAY_NAME_TO_OFFSET.get(day_name)
                if offset is not None:
                    current_monday = now_dt.date() - timedelta(days=now_dt.weekday())
                    sched_msg = format_day_like_today(current_monday + timedelta(days=offset), day_name.capitalize())
            if sched_msg:
                await update.message.reply_text(sched_msg, parse_mode="Markdown", disable_web_page_preview=True)

    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e}\nRaw AI response: {raw_text[:500] if 'raw_text' in locals() else 'N/A'}")
        await processing_msg.edit_text("Не вдалося обробити запит. Спробуй ще раз або переформулюй.")
    except Exception as e:
        await processing_msg.edit_text(f"❌ Помилка при обробці запиту: {str(e)}")

def add_user_if_not_exists(user_id: int, username: str):
    sql = "INSERT INTO users (user_id, username, subscribed) VALUES (%s, %s, 1) ON CONFLICT (user_id) DO NOTHING"
    with get_db_conn() as conn:
        with conn.cursor() as cursor: cursor.execute(sql, (user_id, username))
        conn.commit()

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user_if_not_exists(user.id, user.username)
    text = "Привіт!\nЯ твій розумний AI-асистент з розкладу.\n\n/all - Розклад на тиждень\n/today - На сьогодні\n/manage - Управління\n/randomfact - Отримати ІТ-факт\n\nАбо просто запитай мене: 'Яка в мене завтра перша пара?'"
    await update.message.reply_text(text, parse_mode="Markdown")

async def manage_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    current_week = "парний" if get_current_week_type() == "парна" else "непарний"
    msg = format_pairs_message(get_all_pairs(ADMIN_ID), f"⚙️ Управління розкладом\n(Зараз: **{current_week}** тиждень)\n\n🗓️ Весь розклад (з ID)")
    await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)

async def all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TIMEZONE)
    current_week = "парний" if get_current_week_type() == "парна" else "непарний"
    msg = format_pairs_message(get_schedule_for_current_week(ADMIN_ID, now.date() - timedelta(days=now.weekday())), f"🗓️ Розклад на **{current_week.upper()}** тиждень")
    await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)

async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TIMEZONE)
    weekday = now.weekday()
    if weekday >= 5: return await update.message.reply_text("🔵 На сьогодні\n\n🎉 Вихідний!", parse_mode="Markdown")

    current_day_name = DAY_OF_WEEK_UKR[weekday]
    current_week = get_current_week_type()
    pairs = get_pairs_for_day(ADMIN_ID, current_day_name, current_week)
    title = f"🔵 На сьогодні ({current_day_name.capitalize()}, {'парний' if current_week == 'парна' else 'непарний'} тиждень)"
    await update.message.reply_text(format_pairs_message(pairs, title), parse_mode="Markdown", disable_web_page_preview=True)

async def randomfact_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action(ChatAction.TYPING)
    fact = await generate_unique_fact(update.effective_user.id)
    await update.message.reply_text(f"🎲 **Цікавий ІТ-факт:**\n\n{fact}", parse_mode="Markdown")

# ==========================================
# ЗАПУСК ТА ВЕБХУК
# ==========================================
@asynccontextmanager
async def lifespan(app: Flask):
    global application, main_loop
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("all", all_command))
    application.add_handler(CommandHandler("manage", manage_command))
    application.add_handler(CommandHandler("today", today_command))
    application.add_handler(CommandHandler("randomfact", randomfact_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ai_text_handler))

    await application.initialize()
    main_loop = asyncio.get_running_loop()

    if WEBHOOK_URL:
        await application.bot.set_webhook(f"{WEBHOOK_URL}/webhook/{BOT_TOKEN}", allowed_updates=Update.ALL_TYPES)

    init_db()

    scheduler.init_app(flask_app)
    scheduler.add_job(id='RemindersJob', func=scheduled_job_wrapper, trigger='interval', minutes=1)
    scheduler.start()

    yield

app = Flask(__name__)
flask_app = app

_processing_updates: set = set()

@app.route('/')
def health_check(): return "OK", 200

@app.route(f'/webhook/{BOT_TOKEN}', methods=['POST'])
async def webhook():
    data = flask_request.get_json()
    update = Update.de_json(data, application.bot)
    update_id = update.update_id

    # Якщо Telegram повторно надсилає той самий апдейт (бо сервер не відповів вчасно) — ігноруємо
    if update_id in _processing_updates:
        return "OK", 200

    _processing_updates.add(update_id)

    async def _process_and_cleanup():
        try:
            await application.process_update(update)
        finally:
            _processing_updates.discard(update_id)

    # Повертаємо 200 одразу, щоб Telegram не робив retry при довгих AI-запитах
    asyncio.create_task(_process_and_cleanup())
    return "OK", 200

wsgi_app = WsgiToAsgi(app)

class LifespanMiddleware:
    def __init__(self, app_to_run, lifespan_context, flask_app_instance):
        self.app_to_run = app_to_run
        self.lifespan_context = lifespan_context
        self.flask_app_instance = flask_app_instance
    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            async with self.lifespan_context(self.flask_app_instance):
                await self.app_to_run(scope, receive, send)
        else:
            await self.app_to_run(scope, receive, send)
    
app = LifespanMiddleware(wsgi_app, lifespan, app)