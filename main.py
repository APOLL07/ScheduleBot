# -*- coding: utf-8 -*-
import asyncio
import locale
import os
import psycopg2
import psycopg2.extras
import pytz
from flask import Flask, request as flask_request, abort
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes, TypeHandler
from datetime import datetime, time, timedelta
from asgiref.wsgi import WsgiToAsgi

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
TRIGGER_SECRET = os.environ.get("TRIGGER_SECRET")

if not BOT_TOKEN:
    print("ПОМИЛКА: BOT_TOKEN не знайдено! Перевірте змінні на Render.")
if not DATABASE_URL:
    print("ПОМИЛКА: DATABASE_URL не знайдено! Перевірте змінні на Render.")
if not TRIGGER_SECRET:
    print("ПОМИЛКА: TRIGGER_SECRET не знайдено! Перевірте змінні на Render.")

MY_ID = 1084493666
ADMIN_ID = MY_ID
REMIND_BEFORE_MINUTES = 10
TIMEZONE = pytz.timezone('Europe/Kiev')

REFERENCE_DATE = datetime(2025, 9, 1).date()
REFERENCE_WEEK_TYPE = "непарний"

flask_app = Flask(__name__)
app = WsgiToAsgi(flask_app)
application = Application.builder().token(BOT_TOKEN).build() if BOT_TOKEN else None
_app_initialized = False


# Connects to the PostgreSQL database.
def get_db_conn():
    return psycopg2.connect(DATABASE_URL, sslmode='require', cursor_factory=psycopg2.extras.DictCursor)


# Updates the database schema (adds columns/tables) without deleting data.
def update_db_schema():
    conn = get_db_conn()
    try:
        with conn.cursor() as cursor:
            try:
                cursor.execute(
                    "ALTER TABLE schedule ADD COLUMN week_type TEXT NOT NULL DEFAULT 'кожна'")
                print("Оновлено схему: Додано 'week_type' до 'schedule'")
            except psycopg2.Error as e:
                if e.pgcode == '42701':
                    pass
                else:
                    raise

            cursor.execute('''CREATE TABLE IF NOT EXISTS sent_notifications
                              (
                                  notification_key
                                  TEXT
                                  PRIMARY
                                  KEY,
                                  sent_at
                                  TIMESTAMP
                                  WITH
                                  TIME
                                  ZONE
                                  NOT
                                  NULL
                              )''')
            print("Оновлено схему: Таблиця 'sent_notifications' готова.")

        conn.commit()
    except Exception as e:
        print(f"ПОМИЛКА оновлення схеми: {e}")
        conn.rollback()
    finally:
        conn.close()


# Initializes the core database tables if they do not exist.
def init_db():
    if not DATABASE_URL:
        print("Неможливо ініціалізувати БД: DATABASE_URL не встановлено.")
        return
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute('''CREATE TABLE IF NOT EXISTS schedule
                                  (
                                      id
                                      SERIAL
                                      PRIMARY
                                      KEY,
                                      user_id
                                      BIGINT
                                      NOT
                                      NULL,
                                      day
                                      TEXT
                                      NOT
                                      NULL,
                                      time
                                      TEXT
                                      NOT
                                      NULL,
                                      name
                                      TEXT
                                      NOT
                                      NULL,
                                      link
                                      TEXT,
                                      week_type
                                      TEXT
                                      NOT
                                      NULL
                                      DEFAULT
                                      'кожна'
                                  )''')
                cursor.execute('''CREATE TABLE IF NOT EXISTS users
                                  (
                                      user_id
                                      BIGINT
                                      PRIMARY
                                      KEY,
                                      username
                                      TEXT,
                                      subscribed
                                      INTEGER
                                      DEFAULT
                                      1
                                  )''')
            conn.commit()
        print("Базу даних ініціалізовано (PostgreSQL)")

        update_db_schema()

    except Exception as e:
        print(f"ПОМИЛКА init_db: {e}")


# Adds a new schedule entry to the database.
def add_pair_to_db(user_id: int, day: str, time_str: str, name: str, link: str, week_type: str):
    sql = "INSERT INTO schedule (user_id, day, time, name, link, week_type) VALUES (%s, %s, %s, %s, %s, %s)"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (user_id, day, time_str, name, link, week_type))
        conn.commit()


# Fetches all schedule entries for a specific user, day, and week type.
def get_pairs_for_day(user_id: int, day: str, week_type: str):
    sql = "SELECT * FROM schedule WHERE user_id=%s AND day=%s AND (week_type='кожна' OR week_type=%s) ORDER BY time ASC"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (user_id, day.lower(), week_type))
            rows = cursor.fetchall()
    return rows


# Fetches all schedule entries for a specific user.
def get_all_pairs(user_id: int):
    sql = "SELECT * FROM schedule WHERE user_id=%s ORDER BY week_type, day, time ASC"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (user_id,))
            rows = cursor.fetchall()
    return rows


# Deletes a specific schedule entry by its ID and user ID.
def delete_pair_from_db(pair_id: int, user_id: int):
    sql = "DELETE FROM schedule WHERE id=%s AND user_id = %s"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (pair_id, user_id))
            changes = cursor.rowcount
        conn.commit()
    return changes > 0


# Adds a new user to the users table if they don't already exist.
def add_user_if_not_exists(user_id: int, username: str):
    sql = "INSERT INTO users (user_id, username, subscribed) VALUES (%s, %s, 1) ON CONFLICT (user_id) DO NOTHING"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (user_id, username))
        conn.commit()


# Updates the subscription status (1 or 0) for a user.
def set_user_subscription(user_id: int, subscribed: int):
    sql = "UPDATE users SET subscribed = %s WHERE user_id = %s"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (subscribed, user_id))
        conn.commit()


# Retrieves a list of user IDs for all subscribed users.
def get_all_subscribed_users():
    sql = "SELECT user_id FROM users WHERE subscribed = 1"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql)
            user_ids = [row[0] for row in cursor.fetchall()]
    return user_ids


# Checks if a notification has already been sent today.
def check_if_notified(notification_key: str):
    sql = "SELECT 1 FROM sent_notifications WHERE notification_key = %s"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (notification_key,))
            return cursor.fetchone() is not None


# Marks a notification as sent in the database.
def mark_as_notified(notification_key: str):
    sql = "INSERT INTO sent_notifications (notification_key, sent_at) VALUES (%s, %s)"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (notification_key, datetime.now(TIMEZONE)))
        conn.commit()


# Removes notification records older than 2 days.
def cleanup_old_notifications():
    sql = "DELETE FROM sent_notifications WHERE sent_at < %s"
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cursor:
                cutoff_date = datetime.now(TIMEZONE) - timedelta(days=2)
                cursor.execute(sql, (cutoff_date,))
                deleted_count = cursor.rowcount
            conn.commit()
            if deleted_count > 0:
                print(f"[Cleanup] Видалено {deleted_count} старих сповіщень.")
    except Exception as e:
        print(f"ПОМИЛКА cleanup_old_notifications: {e}")


# Calculates the current week type (e.g., 'odd'/'even') based on the reference date.
def get_current_week_type():
    today = datetime.now(TIMEZONE).date()
    days_diff = (today - REFERENCE_DATE).days
    weeks_diff = days_diff // 7

    if weeks_diff % 2 == 0:
        return REFERENCE_WEEK_TYPE
    else:
        return "парний" if REFERENCE_WEEK_TYPE == "непарний" else "непарний"


# Handles the /start command, registers the user, and shows a welcome message.
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user_if_not_exists(user.id, user.username)
    text = (
        f"Привіт {user.first_name}!\n\n"
        "Я бот з розкладом. Я надсилатиму повідомлення про пари за декілька хвилин.\n\n"
        "**Команди:**\n"
        "/all - Показати весь розклад\n"
        "/today - Показати розклад на сьогодні\n"
        "/subscribe - Увімкнути сповіщення\n"
        "/unsubscribe - Вимкнути сповіщення\n"
        "/help - Довідка\n"
    )
    if user.id == ADMIN_ID:
        text += ("\n**Панель адміну:**\n"
                 "/add `[тип] [день] [час] [назва] [посилання]`\n"
                 "/del `[номер]`")
    await update.message.reply_text(text, parse_mode="Markdown")


# Handles the /help command, showing a list of available commands.
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    text = (
        "**Довідка по командам бота:**\n\n"
        "**/start** - Початок роботи та вітання.\n"
        "**/all** - Показати *весь* розклад на тиждень.\n"
        "**/today** - Показати розклад на *сьогодні* (з урахуванням парного/непарного тижня).\n"
        "**/subscribe** - Увімкнути сповіщення про пари (за замовчуванням).\n"
        "**/unsubscribe** - Вимкнути сповіщення.\n"
        "**/help** - Показати це повідомлення.\n"
    )
    if user_id == ADMIN_ID:
        text += (
            "\n**Панель адміну:**\n"
            "**/add** `[тип] [день] [час] [назва] [посилання]`\n"
            "*Типи: `парна`, `непарна`, `кожна`*\n"
            "*(Приклад: /add парна понеділок 10:00 Математика https://...)*\n\n"
            "**/del** `[ID]`\n"
            "*(ID можна побачити у команді /all)*"
        )
    await update.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)


# Handles the /subscribe command, enabling notifications for the user.
async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_user_subscription(update.effective_chat.id, 1)
    await update.message.reply_text("✅ Сповіщення увімкнено!")


# Handles the /unsubscribe command, disabling notifications for