# -*- coding: utf-8 -*-
import asyncio
import locale
import os
import psycopg2
import psycopg2.extras
from flask import Flask, request as flask_request, abort
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes, TypeHandler
from datetime import datetime, time, timedelta

# --- НАСТРОЙКА ПЕРЕМЕННЫХ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
TRIGGER_SECRET = os.environ.get("TRIGGER_SECRET")

# Перевірка, чи завантажились змінні
if not BOT_TOKEN:
    print("ПОМИЛКА: BOT_TOKEN не знайдено! Перевірте змінні на Render.")
if not DATABASE_URL:
    print("ПОМИЛКА: DATABASE_URL не знайдено! Перевірте змінні на Render.")
if not TRIGGER_SECRET:
    print("ПОМИЛКА: TRIGGER_SECRET не знайдено! Перевірте змінні на Render.")

MY_ID = 1084493666
ADMIN_ID = MY_ID
REMIND_BEFORE_MINUTES = 10

# --- ИНИЦИАЛИЗАЦИЯ FLASK И TELEGRAM ---
flask_app = Flask(__name__)
application = Application.builder().token(BOT_TOKEN).build() if BOT_TOKEN else None


# --- ФУНКЦИИ БАЗЫ ДАННЫХ (ПЕРЕПИСАНЫ ПОД POSTGRESQL) ---

# Вспомогательная функция для подключения к БД
def get_db_conn():
    return psycopg2.connect(DATABASE_URL, sslmode='require', cursor_factory=psycopg2.extras.DictCursor)


# Инициализирует базу данных и создает таблицы.
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
                                      TEXT
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
        print("База данных инициализирована (PostgreSQL)")
    except Exception as e:
        print(f"ПОМИЛКА init_db: {e}")


# Добавляет нову пару до бази даних.
def add_pair_to_db(user_id: int, day: str, time_str: str, name: str, link: str):
    sql = "INSERT INTO schedule (user_id, day, time, name, link) VALUES (%s, %s, %s, %s, %s)"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (user_id, day, time_str, name, link))
        conn.commit()


# Отримує всі пари з БД для конкретного користувача та дня.
def get_pairs_for_day(user_id: int, day: str):
    sql = "SELECT * FROM schedule WHERE user_id=%s AND day=%s ORDER BY time ASC"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (user_id, day.lower()))
            rows = cursor.fetchall()
    return rows


# Отримує абсолютно всі пари для конкретного користувача.
def get_all_pairs(user_id: int):
    sql = "SELECT * FROM schedule WHERE user_id=%s ORDER BY day, time ASC"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (user_id,))
            rows = cursor.fetchall()
    return rows


# Видаляє пару з бази даних за її ID.
def delete_pair_from_db(pair_id: int, user_id: int):
    sql = "DELETE FROM schedule WHERE id=%s AND user_id = %s"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (pair_id, user_id))
            changes = cursor.rowcount
        conn.commit()
    return changes > 0


# Додає нового користувача до БД, якщо він відсутній. (ON CONFLICT - фишка Postgres)
def add_user_if_not_exists(user_id: int, username: str):
    sql = "INSERT INTO users (user_id, username, subscribed) VALUES (%s, %s, 1) ON CONFLICT (user_id) DO NOTHING"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (user_id, username))
        conn.commit()


# Оновлює статус підписки користувача (1 - підписаний, 0 - ні).
def set_user_subscription(user_id: int, subscribed: int):
    sql = "UPDATE users SET subscribed = %s WHERE user_id = %s"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (subscribed, user_id))
        conn.commit()


# Отримує список ID всіх користувачів, які підписані на розсилку.
def get_all_subscribed_users():
    sql = "SELECT user_id FROM users WHERE subscribed = 1"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql)
            user_ids = [row[0] for row in cursor.fetchall()]
    return user_ids


# --- ОБРАБОТЧИКИ КОМАНД TELEGRAM (КОД НЕ ИЗМЕНИЛСЯ) ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user_if_not_exists(user.id, user.username)
    text = (
        f"Привіт {user.first_name}!\n\n"
        "Я бот з розкладом. Я надсилатиму повідомлення про пари за 10 хвилин.\n\n"
        "**Команди:**\n"
        "/all - Показати весь розклад\n"
        "/today - Показати розклад на сьогодні\n"
        "/subscribe - Увімкнути сповіщення\n"
        "/unsubscribe - Вимкнути повідомлення\n"
        "/help - Довідка\n"
    )
    if user.id == ADMIN_ID:
        text += ("\n**Панель адміну:**\n"
                 "/add `[день] [час] [назва] [посилання(опціонально)]`\n"
                 "/del `[номер]`")
    await update.message.reply_text(text, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    text = (
        "**Довідка по командам бота:**\n\n"
        "**/start** - Початок роботи та вітання.\n"
        "**/all** - Показати *весь* розклад на тиждень.\n"
        "**/today** - Показати розклад на *сьогодні*.\n"
        "**/subscribe** - Увімкнути сповіщення про пари (за замовчуванням).\n"
        "**/unsubscribe** - Вимкнути сповіщення.\n"
        "**/help** - Показати це повідомлення.\n"
    )
    if user_id == ADMIN_ID:
        text += (
            "\n**Панель адміну:**\n"
            "**/add** `[день] [час] [назва] [посилання]`\n"
            "*(Приклад: /add понеділок 10:00 Математика https://...)*\n\n"
            "**/del** `[ID]`\n"
            "*(ID можна побачити у команді /all)*"
        )
    await update.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)


async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_user_subscription(update.effective_chat.id, 1)
    await update.message.reply_text("✅ Повідомлення включено!")


async def unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_user_subscription(update.effective_chat.id, 0)
    await update.message.reply_text("❌ Повідомлення вимкнено.")


async def add_para_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ Це команда тільки для адміністратора.")
        return
    if len(context.args) < 3:
        await update.message.reply_text("Формат: `/add [день] [время] [название] [ссылка]`", parse_mode='Markdown')
        return

    day, time_str, name = context.args[0], context.args[1], context.args[2]
    link = context.args[3] if len(context.args) >= 4 else None

    try:
        add_pair_to_db(ADMIN_ID, day.lower(), time_str, name, link)
        await update.message.reply_text(f"✅ Додав пару до *загальний* розклад.")
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка додавання: {e}")


async def show_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    all_pairs = get_all_pairs(ADMIN_ID)
    if not all_pairs:
        await update.message.reply_text("Розклад поки що порожній.")
        return

    message = "📅 **Загальний розклад:**\n"
    current_day = ""
    day_counter = 1

    for para in all_pairs:
        if para['day'] != current_day:
            current_day = para['day']
            message += f"\n**{current_day.capitalize()}**\n"
            day_counter = 1

        prefix = f"`[ID: {para['id']}]` " if user_id == ADMIN_ID else ""
        message += f"{prefix}{day_counter}. `{para['time']}` - {para['name']}\n"

        if para['link']:
            message += f" [Посилання]({para['link']})\n"

        day_counter += 1

    await update.message.reply_text(message, parse_mode="Markdown", disable_web_page_preview=True)


async def show_today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id

    try:
        locale.setlocale(locale.LC_TIME, 'uk_UA.UTF-8')
        current_day = datetime.now().strftime("%A").lower()
    except Exception:
        days_ua = ['понеділок', 'вівторок', 'середа', 'четвер', 'п’ятниця', 'субота', 'неділя']
        current_day = days_ua[datetime.now().weekday()]

    pairs_today = get_pairs_for_day(ADMIN_ID, current_day)

    if not pairs_today:
        await update.message.reply_text(f"Сьогодні ({current_day.capitalize()}) пар немає. Відпочивайте! 🥳")
        return

    message = f"📅 **Розклад на сьогодні ({current_day.capitalize()}):**\n\n"

    for i, para in enumerate(pairs_today):
        prefix = f"`[ID: {para['id']}]` " if user_id == ADMIN_ID else ""
        message += f"{prefix}{i + 1}. `{para['time']}` - {para['name']}\n"

        if para['link']:
            message += f" [Посилання]({para['link']})\n"

    await update.message.reply_text(message, parse_mode="Markdown", disable_web_page_preview=True)


async def del_para_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ Це команда тільки для адміністратора.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Потрібно вказати номер (ID) пари. Приклад: `/del 12`")
        return

    pair_id = int(context.args[0])

    if delete_pair_from_db(pair_id, ADMIN_ID):
        await update.message.reply_text(f"✅ Вилучив пару з ID: {pair_id}")
    else:
        await update.message.reply_text(f"❌ Не знайшов пару з цим ID у загальному розкладі.")


# --- РАССЫЛКА (ТЕПЕРЬ ПРИНИМАЕТ 'application' ЧТОБЫ ПОЛУЧИТЬ БОТА) ---
already_notified = {}


async def check_schedule_and_broadcast(app: Application):
    bot = app.bot

    try:
        locale.setlocale(locale.LC_TIME, 'uk_UA.UTF-8')
        current_day = datetime.now().strftime("%A").lower()
    except Exception:
        days_ua = ['понеділок', 'вівторок', 'середа', 'четвер', 'п’ятниця', 'субота', 'неділя']
        current_day = days_ua[datetime.now().weekday()]

    current_time = datetime.now().strftime("%H:%M")

    print(f"[Розсилання] Перевірка... {current_day} {current_time}")

    try:
        pairs_today = get_pairs_for_day(ADMIN_ID, current_day)
    except Exception as e:
        print(f"ПОМИЛКА check_schedule_and_broadcast (get_pairs_for_day): {e}")
        return

    if not pairs_today:
        return

    for para in pairs_today:
        para_time_str = para['time']
        para_time = datetime.strptime(para_time_str, "%H:%M").time()
        remind_time = (datetime.combine(datetime.now().date(), para_time) - timedelta(
            minutes=REMIND_BEFORE_MINUTES)).time()
        notification_key = f"{current_day}_{para_time_str}"

        if current_time == remind_time.strftime("%H:%M"):
            if notification_key not in already_notified:
                subscribed_users = get_all_subscribed_users()
                if not subscribed_users:
                    print("[Розсилання] Є пара, але немає передплатників.")
                    continue

                message = (
                    f"🔔 **Нагадування!**\n\n"
                    f"Через {REMIND_BEFORE_MINUTES} хвилин ({para_time_str}) у вас є пара:\n\n"
                    f"**{para['name']}**\n\n"
                )
                if para['link']:
                    message += f"🔗 [Посилання на пару]({para['link']})"

                print(f"[Розсилка] Надсилаю '{para['name']}' {len(subscribed_users)} користувачам...")

                for user_id in subscribed_users:
                    try:
                        await bot.send_message(
                            chat_id=user_id, text=message, parse_mode="Markdown")
                    except Exception as e:
                        print(f"[Розсилання] Помилка надсилання {user_id}: {e}. Відписую його.")
                        if "blocked" in str(e) or "deactivated" in str(e):
                            set_user_subscription(user_id, 0)

                already_notified[notification_key] = True

        if current_time > para_time.strftime('%H:%M') and notification_key in already_notified:
            del already_notified[notification_key]


# --- FLASK WEBHOOK-СЕРВЕР ---

@flask_app.route("/", methods=["GET"])
def index():
    return "Bot is alive!", 200


@flask_app.route(f"/trigger_check/{TRIGGER_SECRET}", methods=["POST", "GET"])
async def trigger_check():
    if application:
        await check_schedule_and_broadcast(application)
        return "Check triggered", 200
    return "Bot not initialized", 500


@flask_app.route("/webhook", methods=["POST"])
async def webhook():
    if not application:
        return "Bot not initialized", 500
    try:
        # 2. ВИПРАВЛЕНО: Використовуємо de_json замість from_json
        update_json = flask_request.get_json()
        update = Update.de_json(update_json, application.bot)
        await application.process_update(update)
        return "", 200
    except Exception as e:
        print(f"Ошибка в вебхуке: {e}")
        return "", 500


# --- ГЛАВНАЯ ФУНКЦИЯ ---

# 1. ВИПРАВЛЕНО: Викликаємо init_db() тут, щоб Gunicorn її побачив
# Налаштовуємо локаль (з фоллбэком)
try:
    locale.setlocale(locale.LC_ALL, "uk_UA.UTF-8")
except locale.Error:
    print("ПОПЕРЕДЖЕННЯ: Локаль 'uk_UA.UTF-8' не встановлена. Використовую фоллбэк.")

# Ініціалізуємо БД при старті
init_db()

# Додаємо всі обробники команд
if application:
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("subscribe", subscribe_command))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe_command))
    application.add_handler(CommandHandler("all", show_all_command))
    application.add_handler(CommandHandler("today", show_today_command))
    application.add_handler(CommandHandler("day", show_today_command))
    application.add_handler(CommandHandler("add", add_para_command))
    application.add_handler(CommandHandler("del", del_para_command))
    print("Бот готов к работе (режим Webhook).")
    print("Ініціалізація Application...")
    asyncio.run(application.initialize())
else:
    print("ПОМИЛКА ЗАПУСКУ: 'application' не було створено. Перевірте BOT_TOKEN.")


# Ця функція main() більше не використовується Gunicorn, але ми її залишаємо
def main():
    pass


# Эта проверка нужна, чтобы gunicorn мог найти flask_app
if __name__ == "__main__":
    main()