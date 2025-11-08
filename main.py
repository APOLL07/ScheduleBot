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
from contextlib import asynccontextmanager

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
if not WEBHOOK_URL:
    print("ПОМИЛКА: WEBHOOK_URL не знайдено! Він потрібен для set_webhook.")

MY_ID = 1084493666
ADMIN_ID = MY_ID
REMIND_BEFORE_MINUTES = 10
TIMEZONE = pytz.timezone('Europe/Kiev')

REFERENCE_DATE = datetime(2025, 9, 1).date()
REFERENCE_WEEK_TYPE = "непарний"

DAY_OF_WEEK_UKR = {
    0: "понеділок",
    1: "вівторок",
    2: "середа",
    3: "четвер",
    4: "п'ятниця",
    5: "субота",
    6: "неділя"
}

# === ИЗМЕНЕНО: Добавлен список для правильной сортировки ===
# Этот список определяет порядок сортировки дней
DAY_ORDER_LIST = [
    "понеділок",
    "вівторок",
    "середа",
    "четвер",
    "п'ятниця",
    "субота",
    "неділя"
]

flask_app = None
application = None


def get_db_conn():
    """Підключається до бази даних PostgreSQL."""
    return psycopg2.connect(DATABASE_URL, sslmode='require', cursor_factory=psycopg2.extras.DictCursor)


def update_db_schema():
    """Оновлює схему бази даних (додає стовпці/таблиці), не видаляючи дані."""
    update_week_type_column()
    create_sent_notifications_table()


def update_week_type_column():
    """Додає стовпець 'week_type' до таблиці 'schedule', якщо він не існує."""
    conn = get_db_conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE schedule ADD COLUMN IF NOT EXISTS week_type TEXT NOT NULL DEFAULT 'кожна'")
            print("Оновлено схему: Додано 'week_type' до 'schedule' (якщо не існувало)")
        conn.commit()
    except psycopg2.Error as e:
        if e.pgcode == '42701':
            print("Схема: 'week_type' вже існує, пропускаємо.")
        else:
            print(f"ПОМИЛКА ALTER week_type: {e}")
        conn.rollback()
    finally:
        conn.close()


def create_sent_notifications_table():
    """Створює таблицю 'sent_notifications', якщо вона не існує."""
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


def init_db():
    """Ініціалізує основні таблиці бази даних ('schedule', 'users'), якщо вони не існують."""
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
    """Додає новий запис про пару до бази даних."""
    sql = "INSERT INTO schedule (user_id, day, time, name, link, week_type) VALUES (%s, %s, %s, %s, %s, %s)"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (user_id, day.lower(), time_str, name, link, week_type))
        conn.commit()


def get_pairs_for_day(user_id: int, day: str, week_type: str):
    """Витягує всі пари для конкретного користувача, дня та типу тижня."""
    sql = "SELECT * FROM schedule WHERE user_id=%s AND day=%s AND (week_type='кожна' OR week_type=%s) ORDER BY time ASC"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (user_id, day.lower(), week_type))
            rows = cursor.fetchall()
    return rows


def get_all_pairs(user_id: int):
    """Витягує всі пари для конкретного користувача."""

    # === ИЗМЕНЕНО: Добавлена сортировка CASE для дней недели ===
    # Это создает "виртуальный" столбец с номером дня недели и сортирует по нему
    day_order_sql_case = " ".join([f"WHEN day = '{day}' THEN {i}" for i, day in enumerate(DAY_ORDER_LIST)])

    sql = f"""
    SELECT *,
           CASE {day_order_sql_case} ELSE 99 END as day_order
    FROM schedule 
    WHERE user_id=%s 
    ORDER BY week_type, day_order, time ASC
    """

    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (user_id,))
            rows = cursor.fetchall()
    return rows


def delete_pair_from_db(pair_id: int, user_id: int):
    """Видаляє конкретну пару за її ID та ID користувача."""
    sql = "DELETE FROM schedule WHERE id=%s AND user_id = %s"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (pair_id, user_id))
            changes = cursor.rowcount
        conn.commit()
    return changes > 0


def add_user_if_not_exists(user_id: int, username: str):
    """Додає нового користувача до таблиці 'users', якщо він ще не існує."""
    sql = "INSERT INTO users (user_id, username, subscribed) VALUES (%s, %s, 1) ON CONFLICT (user_id) DO NOTHING"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (user_id, username))
        conn.commit()


def set_user_subscription(user_id: int, subscribed: int):
    """Оновлює статус підписки (1 або 0) для користувача."""
    sql = "UPDATE users SET subscribed = %s WHERE user_id = %s"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (subscribed, user_id))
        conn.commit()


def get_all_subscribed_users():
    """Повертає список ID усіх користувачів, які підписані на сповіщення."""
    sql = "SELECT user_id FROM users WHERE subscribed = 1"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql)
            user_ids = [row[0] for row in cursor.fetchall()]
    return user_ids


def check_if_notified(notification_key: str):
    """Перевіряє, чи було сповіщення з таким ключем вже надіслано."""
    sql = "SELECT 1 FROM sent_notifications WHERE notification_key = %s"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (notification_key,))
            return cursor.fetchone() is not None


def mark_as_notified(notification_key: str):
    """Позначає сповіщення як надіслане в базі даних."""
    sql = "INSERT INTO sent_notifications (notification_key, sent_at) VALUES (%s, %s)"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (notification_key, datetime.now(TIMEZONE)))
        conn.commit()


def cleanup_old_notifications():
    """Видаляє записи про сповіщення, старіші за 2 дні."""
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


def get_current_week_type():
    """
    Визначає тип поточного тижня, повертаючи "парна" або "непарна"
    (у жіночому роді, щоб відповідати команді /add).
    """
    today = datetime.now(TIMEZONE).date()
    days_diff = (today - REFERENCE_DATE).days
    weeks_diff = days_diff // 7

    is_reference_week = (weeks_diff % 2 == 0)

    current_week_type_male = ""
    if is_reference_week:
        current_week_type_male = REFERENCE_WEEK_TYPE
    else:
        current_week_type_male = "парний"

    if current_week_type_male == "парний":
        return "парна"
    else:
        return "непарна"


def format_pairs_message(pairs, title):
    """Допоміжна функція для гарного форматування списку пар."""
    if not pairs:
        return f"{title}\n\n🎉 Пар немає!"

    message = f"{title}\n"
    current_week_type = ""
    current_day = ""
    pair_counter = 0  # === ИЗМЕНЕНО: Добавлен счетчик для нумерации ===

    for pair in pairs:
        if pair['week_type'] != current_week_type and 'весь' in title.lower():
            current_week_type = pair['week_type']

            display_week_type = ""
            if current_week_type == "парна":
                display_week_type = "ПАРНИЙ"
            elif current_week_type == "непарна":
                display_week_type = "НЕПАРНИЙ"
            elif current_week_type == "кожна":
                display_week_type = "КОЖЕН"
            else:
                display_week_type = current_week_type.upper()

            message += f"\n--- **{display_week_type} ТИЖДЕНЬ** ---\n"
            current_day = ""

        if pair['day'] != current_day:
            current_day = pair['day']
            pair_counter = 0  # === ИЗМЕНЕНО: Сброс счетчика на новый день ===

            # === ИЗМЕНЕНО: Не показываем день для /today ===
            if 'сьогодні' not in title.lower():
                message += f"\n**{current_day.capitalize()}**\n"

        pair_counter += 1  # === ИЗМЕНЕНО: Увеличение счетчика ===
        link = f" ([Link]({pair['link']}))" if pair['link'] and pair['link'] != 'None' else ""

        # === ИЗМЕНЕНО: Добавлена нумерация и `time` в `backticks` ===
        message += f"  {pair_counter}) `{pair['time']}` - {pair['name']}{link}\n"

        if 'весь' in title.lower():
            message += f"     *(ID: `{pair['id']}`)*\n"

    return message


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробляє команду /start, реєструє користувача та показує вітальне повідомлення."""
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
    """Обробляє команду /help, показуючи список доступних команд."""
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
    """Обробляє команду /subscribe, вмикаючи сповіщення для користувача."""
    set_user_subscription(update.effective_chat.id, 1)
    await update.message.reply_text("✅ Сповіщення увімкнено!")


async def unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробляє команду /unsubscribe, вимикаючи сповіщення для користувача."""
    set_user_subscription(update.effective_chat.id, 0)
    await update.message.reply_text("❌ Сповіщення вимкнено.")


async def all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показує ВЕСЬ розклад, згрупований по тижнях та днях."""
    user_id = update.effective_chat.id
    try:
        current_week_female = get_current_week_type()
        current_week_male = "парний" if current_week_female == "парна" else "непарний"
        message_header = f"(Зараз: **{current_week_male}** тиждень)\n\n"

        all_pairs = get_all_pairs(user_id)
        title = "🗓️ Весь розклад"

        message_body = format_pairs_message(all_pairs, title)
        await update.message.reply_text(message_header + message_body, parse_mode="Markdown",
                                        disable_web_page_preview=True)
    except Exception as e:
        print(f"ПОМИЛКА в /all: {e}")
        await update.message.reply_text(f"Сталася помилка при отриманні розкладу: {e}")


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показує розклад на СЬОГОДНІ, враховуючи тип тижня."""
    user_id = update.effective_chat.id
    try:
        now = datetime.now(TIMEZONE)
        current_day_name = DAY_OF_WEEK_UKR[now.weekday()]
        current_week_female = get_current_week_type()

        pairs_today = get_pairs_for_day(user_id, current_day_name, current_week_female)

        current_week_male = ""
        if current_week_female == "парна":
            current_week_male = "парний"
        elif current_week_female == "непарна":
            current_week_male = "непарний"

        title = f"🔵 Розклад на сьогодні ({current_day_name.capitalize()}, {current_week_male} тиждень)"
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
            datetime.strptime(time_str, '%H:%M')
        except ValueError:
            await update.message.reply_text("Помилка: невірний 'час'. Має бути у форматі `HH:MM` (напр. `08:30`).")
            return

        if len(args) >= 5:
            link = args[-1]
            name = " ".join(args[3:-1])
            if not link.startswith("http") and link.lower() != 'none':
                name = " ".join(args[3:])
                link = "None"
        else:
            name = " ".join(args[3:])
            link = "None"

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

        if delete_pair_from_db(pair_id, user_id):
            await update.message.reply_text(f"✅ Пару з ID `{pair_id}` видалено.")
        else:
            await update.message.reply_text(f"❌ Не вдалося знайти пару з ID `{pair_id}`, що належить вам.")

    except ValueError:
        await update.message.reply_text("Помилка: ID має бути числом.")
    except Exception as e:
        print(f"ПОМИЛКА в /del: {e}")
        await update.message.reply_text(f"Сталася невідома помилка: {e}")


async def check_and_send_reminders(bot: Bot):
    """
    Головна функція для Cron-завдання.
    Перевіряє розклад та надсилає нагадування.
    """
    print(f"[check_and_send_reminders] Запуск перевірки нагадувань... Час: {datetime.now(TIMEZONE)}")

    try:
        now = datetime.now(TIMEZONE)
        notification_time_dt = now + timedelta(minutes=REMIND_BEFORE_MINUTES)

        target_time_obj = notification_time_dt.time().replace(second=0, microsecond=0)

        current_day_name = DAY_OF_WEEK_UKR[now.weekday()]
        current_week_type = get_current_week_type()

        print(f"[Check] Шукаємо пари на {current_day_name}, {current_week_type} о {target_time_obj.strftime('%H:%M')}")

        subscribed_users = get_all_subscribed_users()
        if not subscribed_users:
            print("[Check] Немає підписаних користувачів.")
            return

        for user_id in subscribed_users:
            pairs_today = get_pairs_for_day(user_id, current_day_name, current_week_type)

            if not pairs_today:
                continue

            for pair in pairs_today:
                try:
                    try:
                        pair_time_obj = datetime.strptime(pair['time'], '%H:%M').time()
                    except ValueError:
                        print(f"ПОМИЛКА: Невірний формат часу в парі {pair['id']}: {pair['time']}")
                        continue

                    if pair_time_obj == target_time_obj:
                        print(f"[Check] Знайдено пару для {user_id}! ID: {pair['id']}")

                        notification_key = f"{user_id}_{pair['id']}_{now.strftime('%Y-%m-%d')}"

                        if not check_if_notified(notification_key):
                            print(f"[Check] Надсилаємо сповіщення {notification_key}...")

                            link = f"\n\nПосилання: {pair['link']}" if pair['link'] and pair['link'] != 'None' else ""
                            message = (
                                f"🔔 **Нагадування!**\n\n"
                                f"Через {REMIND_BEFORE_MINUTES} хвилин ({pair['time']}) почнеться пара:\n"
                                f"**{pair['name']}**"
                                f"{link}"
                            )

                            await bot.send_message(user_id, message, parse_mode="Markdown",
                                                   disable_web_page_preview=True)

                            mark_as_notified(notification_key)
                        else:
                            print(f"[Check] Сповіщення {notification_key} вже було надіслано.")

                except Exception as e_pair:
                    print(f"ПОМИЛКА обробки пари {pair['id']} для user {user_id}: {e_pair}")

        cleanup_old_notifications()

    except Exception as e:
        print(f"КРИТИЧНА ПОМИЛКА в check_and_send_reminders: {e}")
        try:
            await bot.send_message(ADMIN_ID, f"ПОМИЛКА в check_and_send_reminders:\n{e}")
        except Exception as e_admin:
            print(f"Не вдалося навіть надіслати повідомлення адміну: {e_admin}")


@asynccontextmanager
async def lifespan(app: Flask):
    """
    Ця функція запускається Uvicorn ОДИН РАЗ під час старту.
    Це правильне місце для ініціалізації та налаштування вебхука.
    """
    global application, flask_app
    print("Lifespan: Запуск...")

    flask_app = app
    application = Application.builder().token(BOT_TOKEN).build() if BOT_TOKEN else None

    if application:
        print("Lifespan: Реєстрація обробників...")
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("subscribe", subscribe_command))
        application.add_handler(CommandHandler("unsubscribe", unsubscribe_command))
        application.add_handler(CommandHandler("all", all_command))
        application.add_handler(CommandHandler("today", today_command))
        application.add_handler(CommandHandler("add", add_command))
        application.add_handler(CommandHandler("del", del_command))
        print("Lifespan: Обробники зареєстровані.")

        print("Lifespan: Ініціалізація Application (application.initialize)...")
        await application.initialize()
        print("Lifespan: Application ініціалізовано.")

        try:
            if WEBHOOK_URL:
                webhook_path = f"{WEBHOOK_URL}/webhook/{BOT_TOKEN}"
                await application.bot.set_webhook(
                    webhook_path,
                    allowed_updates=Update.ALL_TYPES
                )
                print(f"============================================================")
                print(f"✅ Lifespan: Webhook ВСТАНОВЛЕНО на: {webhook_path}")
                print(f"============================================================")
            else:
                print("❌ Lifespan: ПОМИЛКА, WEBHOOK_URL не знайдено.")
        except Exception as e:
            print(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            print(f"🔥 Lifespan: КРИТИЧНА ПОМИЛКА під час set_webhook: {e}")
            print(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    else:
        print("❌ Lifespan: ПОМИЛКА, 'application' не було створено (немає BOT_TOKEN?)")

    init_db()

    print("Lifespan: Запуск завершено, передаємо керування Uvicorn.")
    yield
    print("Lifespan: Зупинка...")


app = Flask(__name__)


@app.route('/')
def health_check():
    """Маршрут для перевірок Render (прибирає 404)."""
    print("Перевірка працездатності / OK")
    return "OK, Сервіс працює!", 200


@app.route(f'/webhook/{BOT_TOKEN}', methods=['POST'])
async def webhook():
    """Обробляє вхідні оновлення від Telegram."""
    if not application:
        print("ПОМИЛКА: 'application' не ініціалізовано у /webhook.")
        return "Бот не ініціалізовано", 500
    try:
        update_data = flask_request.get_json()
        update = Update.de_json(update_data, application.bot)
        await application.process_update(update)
        return "OK", 200
    except Exception as e:
        print(f"ПОМИЛКА обробки вебхука: {e}")
        return "Помилка", 500


@app.route(f'/trigger/{TRIGGER_SECRET}', methods=['POST'])
async def trigger_reminders():
    """
    Маршрут для Cron-завдання (Render Cron Job).
    Запускає перевірку та надсилання нагадувань.
    """
    if not application:
        print("ПОМИЛКА: 'application' не ініціалізовано у /trigger.")
        return "Бот не ініціалізовано", 500

    auth_header = flask_request.headers.get('Authorization')
    if auth_header != f"Bearer {TRIGGER_SECRET}":
        print(f"ПОМИЛКА: Невірний секрет у /trigger. Отримано: {auth_header}")
        return "Заборонено", 403

    print("[Trigger] Отримано запит на перевірку нагадувань...")
    try:
        asyncio.create_task(check_and_send_reminders(application.bot))
        return "Тригер оброблено", 200
    except Exception as e:
        print(f"ПОМИЛКА тригера: {e}")
        return "Помилка тригера", 500


wsgi_app = WsgiToAsgi(app)


@asynccontextmanager
async def combined_lifespan(app_instance):
    """
    Комбінує наш 'lifespan' з 'lifespan' Flask-додатку.
    """
    async with lifespan(app_instance):
        yield


class LifespanMiddleware:
    def __init__(self, app, lifespan_context):
        self.app = app
        self.lifespan_context = lifespan_context

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            async with self.lifespan_context(self.app):
                await self.app(scope, receive, send)
        else:
            await self.app(scope, receive, send)


app = LifespanMiddleware(wsgi_app, lifespan_context=combined_lifespan)

print("Додаток налаштовано з 'lifespan' та готовий до запуску.")