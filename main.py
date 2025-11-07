# -*- coding: utf-8 -*-
import asyncio
import locale
import os
import psycopg2
import psycopg2.extras
import pytz
from flask import Flask, request as flask_request, abort, jsonify
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes, TypeHandler
from datetime import datetime, time, timedelta
from asgiref.wsgi import WsgiToAsgi

# --- 1. Налаштування та Змінні ---

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
TRIGGER_SECRET = os.environ.get("TRIGGER_SECRET")

if not BOT_TOKEN:
    print("ПОМИЛKA: BOT_TOKEN не знайдено! Перевірте змінні на Render.")
if not DATABASE_URL:
    print("ПОМИЛKA: DATABASE_URL не знайдено! Перевірте змінні на Render.")
if not TRIGGER_SECRET:
    print("ПОМИЛKA: TRIGGER_SECRET не знайдено! Перевірте змінні на Render.")
if not WEBHOOK_URL:
    print("ПОПЕРЕДЖЕННЯ: WEBHOOK_URL не знайдено! Потрібно для налаштування вебхука.")

MY_ID = 1084493666
ADMIN_ID = MY_ID
REMIND_BEFORE_MINUTES = 10
TIMEZONE = pytz.timezone('Europe/Kiev')

REFERENCE_DATE = datetime(2025, 9, 1).date()
REFERENCE_WEEK_TYPE = "непарний"

# --- 2. Ініціалізація Додатків ---
# (Application та Flask ініціалізуються тут, щоб бути доступними глобально)

flask_app = Flask(__name__)
application = Application.builder().token(BOT_TOKEN).build() if BOT_TOKEN else None


# --- 3. Функції Роботи з Базою Даних (PostgreSQL) ---

def get_db_conn():
    """Connects to the PostgreSQL database."""
    return psycopg2.connect(DATABASE_URL, sslmode='require', cursor_factory=psycopg2.extras.DictCursor)


def update_db_schema():
    """Updates the database schema (adds columns/tables) without deleting data."""
    conn = get_db_conn()
    try:
        with conn.cursor() as cursor:
            try:
                cursor.execute(
                    "ALTER TABLE schedule ADD COLUMN week_type TEXT NOT NULL DEFAULT 'кожна'")
                print("Оновлено схему: Додано 'week_type' до 'schedule'")
            except psycopg2.Error as e:
                if e.pgcode == '42701':  # 42701 = duplicate_column
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


def init_db():
    """Initializes the core database tables if they do not exist."""
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


def add_pair_to_db(user_id: int, day: str, time_str: str, name: str, link: str, week_type: str):
    """Adds a new schedule entry to the database."""
    sql = "INSERT INTO schedule (user_id, day, time, name, link, week_type) VALUES (%s, %s, %s, %s, %s, %s)"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (user_id, day, time_str, name, link, week_type))
        conn.commit()


def get_pairs_for_day(user_id: int, day: str, week_type: str):
    """Fetches all schedule entries for a specific user, day, and week type."""
    sql = "SELECT * FROM schedule WHERE user_id=%s AND day=%s AND (week_type='кожна' OR week_type=%s) ORDER BY time ASC"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (user_id, day.lower(), week_type))
            rows = cursor.fetchall()
    return rows


def get_all_pairs(user_id: int):
    """Fetches all schedule entries for a specific user."""
    sql = "SELECT * FROM schedule WHERE user_id=%s ORDER BY week_type, day, time ASC"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (user_id,))
            rows = cursor.fetchall()
    return rows


def delete_pair_from_db(pair_id: int, user_id: int):
    """Deletes a specific schedule entry by its ID and user ID."""
    sql = "DELETE FROM schedule WHERE id=%s AND user_id = %s"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (pair_id, user_id))
            changes = cursor.rowcount
        conn.commit()
    return changes > 0


def add_user_if_not_exists(user_id: int, username: str):
    """Adds a new user to the users table if they don't already exist."""
    sql = "INSERT INTO users (user_id, username, subscribed) VALUES (%s, %s, 1) ON CONFLICT (user_id) DO NOTHING"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (user_id, username))
        conn.commit()


def set_user_subscription(user_id: int, subscribed: int):
    """Updates the subscription status (1 or 0) for a user."""
    sql = "UPDATE users SET subscribed = %s WHERE user_id = %s"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (subscribed, user_id))
        conn.commit()


def get_all_subscribed_users():
    """Retrieves a list of user IDs for all subscribed users."""
    sql = "SELECT user_id FROM users WHERE subscribed = 1"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql)
            user_ids = [row[0] for row in cursor.fetchall()]
    return user_ids


def check_if_notified(notification_key: str):
    """Checks if a notification has already been sent today."""
    sql = "SELECT 1 FROM sent_notifications WHERE notification_key = %s"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (notification_key,))
            return cursor.fetchone() is not None


def mark_as_notified(notification_key: str):
    """Marks a notification as sent in the database."""
    sql = "INSERT INTO sent_notifications (notification_key, sent_at) VALUES (%s, %s)"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (notification_key, datetime.now(TIMEZONE)))
        conn.commit()


def cleanup_old_notifications():
    """Removes notification records older than 2 days."""
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


# --- 4. Логіка Бота (Допоміжні функції) ---

def get_current_week_type():
    """Calculates the current week type (e.g., 'odd'/'even') based on the reference date."""
    today = datetime.now(TIMEZONE).date()
    days_diff = (today - REFERENCE_DATE).days
    weeks_diff = days_diff // 7

    if weeks_diff % 2 == 0:
        return REFERENCE_WEEK_TYPE
    else:
        return "парний" if REFERENCE_WEEK_TYPE == "непарний" else "непарний"


# --- 5. Обробники Команд Telegram ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command, registers the user, and shows a welcome message."""
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


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /help command, showing a list of available commands."""
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


async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /subscribe command, enabling notifications for the user."""
    set_user_subscription(update.effective_chat.id, 1)
    await update.message.reply_text("✅ Сповіщення увімкнено!")


async def unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /unsubscribe command, disabling notifications for the user."""
    set_user_subscription(update.effective_chat.id, 0)
    await update.message.reply_text("❌ Сповіщення вимкнено.")


# --- ЗАГЛУШКИ: Тобі потрібно реалізувати ці функції ---

async def all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(ЗАГЛУШКА) Повинна показувати весь розклад."""
    await update.message.reply_text("Функція /all ще не реалізована.")
    # Тут має бути твій код для показу всього розкладу (використовуй get_all_pairs)


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(ЗАГЛУШКА) Повинна показувати розклад на сьогодні."""
    await update.message.reply_text("Функція /today ще не реалізована.")
    # Тут має бути твій код для показу розкладу на сьогодні (використовуй get_pairs_for_day та get_current_week_type)


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(ЗАГLУШКА) Повинна додавати нову пару (тільки для адміна)."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Ця команда доступна лише адміну.")
        return
    await update.message.reply_text("Функція /add ще не реалізована.")
    # Тут має бути твій код для парсингу context.args та виклику add_pair_to_db


async def del_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(ЗАГЛУШКА) Повинна видаляти пару (тільки для адміна)."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Ця команда доступна лише адміну.")
        return
    await update.message.reply_text("Функція /del ще не реалізована.")
    # Тут має бути твій код для парсингу context.args та виклику delete_pair_from_db


# --- 6. Логіка Нагадувань (для Cron) ---

async def check_and_send_reminders(bot: Bot):
    """
    (ЗАГЛУШКА) Головна функція для Cron-завдання.
    Має перевіряти розклад та надсилати нагадування.
    """
    print("[check_and_send_reminders] Запуск перевірки нагадувань...")
    # 1. Отримати поточний час, день тижня, тип тижня.
    # 2. Отримати всіх підписаних користувачів (get_all_subscribed_users).
    # 3. Для кожного користувача:
    #    a. Отримати його розклад на сьогодні (get_pairs_for_day).
    #    b. Пройтись по парах.
    #    c. Якщо час пари (мінус REMIND_BEFORE_MINUTES) == поточний час:
    #       i. Сформувати ключ (notification_key = f"{user_id}_{pair_id}_{today}").
    #       ii. Перевірити, чи вже надсилали (check_if_notified).
    #       iii. Якщо не надсилали:
    #           - Надіслати повідомлення (bot.send_message).
    #           - Позначити як надіслане (mark_as_notified).
    # 4. Запустити очистку старих нотифікацій (cleanup_old_notifications).

    # Поки що просто надсилаємо повідомлення адміну, що Cron спрацював
    await bot.send_message(ADMIN_ID,
                           "⏰ (TEST) Cron-завдання спрацювало! Функція `check_and_send_reminders` була викликана.")


# --- 7. Маршрути Flask (Вебхуки) ---

@flask_app.route('/')
def health_check():
    """Маршрут для перевірок Render (прибирає 404)."""
    return "OK, Service is alive!", 200


@flask_app.route(f'/webhook/{BOT_TOKEN}', methods=['POST'])
async def webhook():
    """Обробляє вхідні оновлення від Telegram."""
    if not application:
        print("ПОМИЛКА: 'application' не ініціалізовано у /webhook.")
        return "Bot not initialized", 500
    try:
        update_data = flask_request.get_json()
        update = Update.de_json(update_data, application.bot)
        await application.process_update(update)
        return "OK", 200
    except Exception as e:
        print(f"ПОМИЛКА обробки вебхука: {e}")
        return "Error", 500


@flask_app.route(f'/trigger/{TRIGGER_SECRET}', methods=['POST'])
async def trigger_reminders():
    """
    Маршрут для Cron-завдання (Render Cron Job).
    Запускає перевірку та надсилання нагадувань.
    """
    if not application:
        print("ПОМИЛКА: 'application' не ініціалізовано у /trigger.")
        return "Bot not initialized", 500

    secret = flask_request.headers.get('Authorization')
    if secret != f"Bearer {TRIGGER_SECRET}":
        print(f"ПОМИЛКА: Невірний секрет у /trigger. Отримано: {secret}")
        return "Forbidden", 403  # Або abort(403)

    print("[Trigger] Отримано запит на перевірку нагадувань...")
    try:
        await check_and_send_reminders(application.bot)
        return "Trigger processed", 200
    except Exception as e:
        print(f"ПОМИЛКА тригера: {e}")
        return "Trigger Error", 500


# --- 8. Реєстрація Обробників та Запуск ---

if application:
    print("Реєстрація обробників команд...")
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("subscribe", subscribe_command))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe_command))

    # Реєстрація нових команд (поки що "заглушок")
    application.add_handler(CommandHandler("all", all_command))
    application.add_handler(CommandHandler("today", today_command))
    application.add_handler(CommandHandler("add", add_command))
    application.add_handler(CommandHandler("del", del_command))

    print("Обробники зареєстровані.")
else:
    print("ПОМИЛКА: Не вдалося зареєструвати обробники, 'application' - None.")

# Ініціалізуємо БД при старті
init_db()

# Створюємо ASGI-обгортку для Uvicorn
# Uvicorn буде шукати саме цю змінну 'app'
app = WsgiToAsgi(flask_app)