# -*- coding: utf-8 -*-
import asyncio
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

# Перевірки наявності змінних
if not BOT_TOKEN:
    print("ПОМИЛКА: BOT_TOKEN не знайдено! Перевірте змінні на Render.")
if not DATABASE_URL:
    print("ПОМИЛКА: DATABASE_URL не знайдено! Перевірте змінні на Render.")
if not TRIGGER_SECRET:
    print("ПОМИЛКА: TRIGGER_SECRET не знайдено! Перевірте змінні на Render.")
if not WEBHOOK_URL:
    print("ПОПЕРЕДЖЕННЯ: WEBHOOK_URL не знайдено! Потрібно для налаштування вебхука.")

MY_ID = 1084493666
ADMIN_ID = MY_ID
REMIND_BEFORE_MINUTES = 10
TIMEZONE = pytz.timezone('Europe/Kiev')

REFERENCE_DATE = datetime(2025, 9, 1).date()
REFERENCE_WEEK_TYPE = "непарний"

# Словник для перекладу дня тижня (з datetime.weekday())
DAY_OF_WEEK_UKR = {
    0: "понеділок",
    1: "вівторок",
    2: "середа",
    3: "четвер",
    4: "п'ятниця",
    5: "субота",
    6: "неділя"
}

# --- 2. Ініціалізація Додатків ---
flask_app = Flask(__name__)
application = Application.builder().token(BOT_TOKEN).build() if BOT_TOKEN else None


# --- 3. Функції Роботи з Базою Даних (PostgreSQL) ---

# Connects to the PostgreSQL database.
def get_db_conn():
    return psycopg2.connect(DATABASE_URL, sslmode='require', cursor_factory=psycopg2.extras.DictCursor)


# Updates the database schema (adds columns/tables) without deleting data.
def update_db_schema():
    # Separate operations to avoid transaction abortion affecting others
    update_week_type_column()
    create_sent_notifications_table()


def update_week_type_column():
    conn = get_db_conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE schedule ADD COLUMN IF NOT EXISTS week_type TEXT NOT NULL DEFAULT 'кожна'")
            print("Оновлено схему: Додано 'week_type' до 'schedule' (якщо не існувало)")
        conn.commit()
    except psycopg2.Error as e:
        if e.pgcode == '42701':  # 42701 = duplicate_column
            print("Схема: 'week_type' вже існує, пропускаємо.")
        else:
            print(f"ПОМИЛКА ALTER week_type: {e}")
        conn.rollback()
    finally:
        conn.close()


def create_sent_notifications_table():
    conn = get_db_conn()
    try:
        with conn.cursor() as cursor:
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
        print(f"ПОМИЛКА CREATE sent_notifications: {e}")
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
            cursor.execute(sql, (user_id, day.lower(), time_str, name, link, week_type))
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


def format_pairs_message(pairs, title):
    """Допоміжна функція для гарного форматування списку пар."""
    if not pairs:
        return f"{title}\n\n🎉 Пар немає!"

    message = f"{title}\n"
    current_week_type = ""
    current_day = ""

    for pair in pairs:
        # Додаємо заголовки для типу тижня (тільки для /all)
        if pair['week_type'] != current_week_type and 'весь' in title.lower():
            current_week_type = pair['week_type']
            message += f"\n--- **{current_week_type.upper()} ТИЖДЕНЬ** ---\n"
            current_day = ""  # Скидаємо день при зміні тижня

        # Додаємо заголовки для дня
        if pair['day'] != current_day:
            current_day = pair['day']
            message += f"\n**{current_day.capitalize()}**\n"

        # Форматуємо саму пару
        link = f" ([Link]({pair['link']}))" if pair['link'] and pair['link'] != 'None' else ""
        message += f"  `{pair['time']}` - {pair['name']}{link}\n"

        # Додаємо ID для адміна в /all
        if 'весь' in title.lower():
            message += f"     *(ID: `{pair['id']}`)*\n"

    return message


# --- 5. Обробники Команд Telegram (РЕАЛІЗОВАНІ) ---

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
        "**/all** - Показати *весь* розклад на тиждень (з ID для видалення).\n"
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
            "*День: `понеділок`, `вівторок` і т.д.*\n"
            "*Час: `08:30`, `10:00`*\n"
            "*Посилання: `https://...` або `None`*\n"
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


async def all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показує ВЕСЬ розклад, згрупований по тижнях та днях."""
    user_id = update.effective_chat.id
    try:
        all_pairs = get_all_pairs(user_id)
        message = format_pairs_message(all_pairs, "🗓️ Весь розклад")
        await update.message.reply_text(message, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        print(f"ПОМИЛКА в /all: {e}")
        await update.message.reply_text(f"Сталася помилка при отриманні розкладу: {e}")


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показує розклад на СЬОГОДНІ, враховуючи тип тижня."""
    user_id = update.effective_chat.id
    try:
        now = datetime.now(TIMEZONE)
        current_day_name = DAY_OF_WEEK_UKR[now.weekday()]
        current_week = get_current_week_type()

        pairs_today = get_pairs_for_day(user_id, current_day_name, current_week)

        title = f"🔵 Розклад на сьогодні ({current_day_name.capitalize()}, {current_week} тиждень)"
        message = format_pairs_message(pairs_today, title)

        await update.message.reply_text(message, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        print(f"ПОМИЛКА в /today: {e}")
        await update.message.reply_text(f"Сталася помилка при отриманні розкладу на сьогодні: {e}")


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Додає нову пару (тільки для адміна)."""
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("⛔ Ця команда доступна лише адміну.")
        return

    args = context.args
    if len(args) < 4:
        await update.message.reply_text(
            "Помилка: Недостатньо аргументів.\n"
            "Формат: /add `[тип] [день] [час] [назва] [посилання (необов'язково)]`\n"
            "Приклад: /add `кожна понеділок 08:30 Англійська https://...`",
            parse_mode="Markdown"
        )
        return

    try:
        # Валідація вхідних даних
        week_type = args[0].lower()
        if week_type not in ['парна', 'непарна', 'кожна']:
            await update.message.reply_text("Помилка: невірний 'тип'. Має бути `парна`, `непарна` або `кожна`.")
            return

        day = args[1].lower()
        if day not in DAY_OF_WEEK_UKR.values():
            await update.message.reply_text(
                f"Помилка: невірний 'день'. Має бути один з: {', '.join(DAY_OF_WEEK_UKR.values())}")
            return

        time_str = args[2]
        try:
            # Просто перевіряємо формат
            datetime.strptime(time_str, '%H:%M')
        except ValueError:
            await update.message.reply_text("Помилка: невірний 'час'. Має бути у форматі `HH:MM` (напр. `08:30`).")
            return

        # Назва може містити пробіли, тому беремо все до останнього аргументу
        # Якщо 5+ аргументів, останній - посилання. Якщо 4 - посилання немає.
        if len(args) >= 5:
            link = args[-1]
            name = " ".join(args[3:-1])
            if not link.startswith("http") and link.lower() != 'none':
                # Якщо 5-й аргумент не схожий на посилання, це частина назви
                name = " ".join(args[3:])
                link = "None"
        else:
            name = " ".join(args[3:])
            link = "None"

        # Додаємо в БД
        add_pair_to_db(user_id, day, time_str, name, link, week_type)

        await update.message.reply_text(
            f"✅ *Пару додано:*\n"
            f"Тип: {week_type}\n"
            f"День: {day}\n"
            f"Час: {time_str}\n"
            f"Назва: {name}\n"
            f"Посилання: {link}",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )

    except Exception as e:
        print(f"ПОМИЛКА в /add: {e}")
        await update.message.reply_text(f"Сталася невідома помилка: {e}")


async def del_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Видаляє пару за ID (тільки для адміна)."""
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("⛔ Ця команда доступна лише адміну.")
        return

    if not context.args or len(context.args) != 1:
        await update.message.reply_text("Помилка: Вкажіть ID пари для видалення.\n"
                                        "Приклад: /del `12`\n"
                                        "(ID можна побачити у команді /all)")
        return

    try:
        pair_id = int(context.args[0])

        # Видаляємо з БД
        if delete_pair_from_db(pair_id, user_id):
            await update.message.reply_text(f"✅ Пару з ID `{pair_id}` видалено.")
        else:
            await update.message.reply_text(f"❌ Не вдалося знайти пару з ID `{pair_id}`, що належить вам.")

    except ValueError:
        await update.message.reply_text("Помилка: ID має бути числом.")
    except Exception as e:
        print(f"ПОМИЛКА в /del: {e}")
        await update.message.reply_text(f"Сталася невідома помилка: {e}")


# --- 6. Логіка Нагадувань (для Cron) ---

async def check_and_send_reminders(bot: Bot):
    """
    Головна функція для Cron-завдання.
    Перевіряє розклад та надсилає нагадування.
    """
    print(f"[check_and_send_reminders] Запуск перевірки нагадувань... Час: {datetime.now(TIMEZONE)}")

    try:
        # 1. Отримуємо всі необхідні дані про поточний час
        now = datetime.now(TIMEZONE)
        # Час, коли має початися пара (зараз + X хвилин)
        notification_time_dt = now + timedelta(minutes=REMIND_BEFORE_MINUTES)

        # Округлюємо час до хвилини
        target_time_obj = notification_time_dt.time().replace(second=0, microsecond=0)

        current_day_name = DAY_OF_WEEK_UKR[now.weekday()]
        current_week_type = get_current_week_type()

        print(f"[Check] Шукаємо пари на {current_day_name}, {current_week_type} о {target_time_obj.strftime('%H:%M')}")

        # 2. Отримуємо всіх підписаних користувачів
        subscribed_users = get_all_subscribed_users()
        if not subscribed_users:
            print("[Check] Немає підписаних користувачів.")
            return

        # 3. Для кожного користувача...
        for user_id in subscribed_users:
            # a. Отримати його розклад на сьогодні
            pairs_today = get_pairs_for_day(user_id, current_day_name, current_week_type)

            if not pairs_today:
                continue  # У цього користувача сьогодні пар немає

            # b. Пройтись по парах
            for pair in pairs_today:
                try:
                    # Додаємо try/except для парсингу часу
                    try:
                        pair_time_obj = datetime.strptime(pair['time'], '%H:%M').time()
                    except ValueError:
                        print(f"ПОМИЛКА: Невірний формат часу в парі {pair['id']}: {pair['time']}")
                        continue

                    # c. Якщо час пари == наш цільовий час
                    if pair_time_obj == target_time_obj:
                        print(f"[Check] Знайдено пару для {user_id}! ID: {pair['id']}")

                        # i. Формуємо ключ (щоб не слати 100 разів, якщо cron бігає кожну сек)
                        # Ключ унікальний для пари, користувача та дня
                        notification_key = f"{user_id}_{pair['id']}_{now.strftime('%Y-%m-%d')}"

                        # ii. Перевіряємо, чи вже надсилали
                        if not check_if_notified(notification_key):
                            print(f"[Check] Надсилаємо сповіщення {notification_key}...")

                            # iii. Надсилаємо повідомлення
                            link = f"\n\nПосилання: {pair['link']}" if pair['link'] and pair['link'] != 'None' else ""
                            message = (
                                f"🔔 **Нагадування!**\n\n"
                                f"Через {REMIND_BEFORE_MINUTES} хвилин ({pair['time']}) почнеться пара:\n"
                                f"**{pair['name']}**"
                                f"{link}"
                            )

                            await bot.send_message(user_id, message, parse_mode="Markdown", disable_web_page_preview=True)

                            # iv. Позначаємо як надіслане
                            mark_as_notified(notification_key)
                        else:
                            print(f"[Check] Сповіщення {notification_key} вже було надіслано.")

                except Exception as e_pair:
                    print(f"ПОМИЛКА обробки пари {pair['id']} для user {user_id}: {e_pair}")

        # 4. Очищуємо старі записи про нотифікації
        cleanup_old_notifications()

    except Exception as e:
        print(f"КРИТИЧНА ПОМИЛКА в check_and_send_reminders: {e}")
        # Повідомляємо адміну про проблему
        try:
            await bot.send_message(ADMIN_ID, f"ПОМИЛКА в check_and_send_reminders:\n{e}")
        except Exception as e_admin:
            print(f"Не вдалося навіть надіслати повідомлення адміну: {e_admin}")


# --- 7. Маршрути Flask (Вебхуки) ---

@flask_app.route('/')
def health_check():
    """Маршрут для перевірок Render (прибирає 404)."""
    print("Health check / OK")
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

    # Проста перевірка секрету (можна передавати в заголовках для більшої безпеки)
    # Наприклад, `Authorization: Bearer <YOUR_TRIGGER_SECRET>`
    auth_header = flask_request.headers.get('Authorization')
    if auth_header != f"Bearer {TRIGGER_SECRET}":
        print(f"ПОМИЛКА: Невірний секрет у /trigger. Отримано: {auth_header}")
        return "Forbidden", 403

    print("[Trigger] Отримано запит на перевірку нагадувань...")
    try:
        # Запускаємо асинхронну функцію у фоні, щоб не блокувати відповідь
        # Це важливо, якщо перевірка триває довго
        asyncio.create_task(check_and_send_reminders(application.bot))
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

    # Реєстрація реалізованих команд
    application.add_handler(CommandHandler("all", all_command))
    application.add_handler(CommandHandler("today", today_command))
    application.add_handler(CommandHandler("add", add_command))
    application.add_handler(CommandHandler("del", del_command))

    print("Обробники зареєстровані.")
else:
    print("ПОМИЛКА: Не вдалося зареєструвати обробники, 'application' - None.")

# Ініціалізуємо БД при старті
init_db()

# Налаштування вебхука (асинхронна версія)
async def set_webhook():
    if WEBHOOK_URL and application:
        webhook_path = f"{WEBHOOK_URL}/webhook/{BOT_TOKEN}"
        await application.bot.set_webhook(webhook_path)
        print(f"Webhook встановлено на: {webhook_path}")
    else:
        print("ПОПЕРЕДЖЕННЯ: Webhook не встановлено, бо WEBHOOK_URL відсутній або application - None.")

# Синхронна обгортка для виклику на рівні модуля (створює новий event loop)
def set_webhook_sync():
    if not application:
        return
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(set_webhook())
    except Exception as e:
        print(f"ПОМИЛКА налаштування вебхука: {e}")
    finally:
        loop.close()

# Викликаємо налаштування вебхука на старті (синхронно)
set_webhook_sync()

# Створюємо ASGI-обгортку для Uvicorn
# Uvicorn буде шукати саме цю змінну 'app'
app = WsgiToAsgi(flask_app)

print("Додаток готовий до запуску через Uvicorn.")