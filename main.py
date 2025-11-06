import asyncio
import sqlite3
import locale
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from datetime import datetime, time ,timedelta

BOT_TOKEN = "8272053633:AAEDcJhlwFGMfzpRf-yiveDld6hvRlg1gC0"
MY_ID = 1084493666
ADMIN_ID = MY_ID
DB_FILE = "schedule.db"
REMIND_BEFORE_MINUTES = 10

# Ініціалізує базу даних та створює таблиці.
def init_db():
    connect = sqlite3.connect(DB_FILE)
    cursor = connect.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS schedule (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    time TEXT NOT NULL,
    name TEXT NOT NULL,
    link TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    subscribed INTEGER DEFAULT 1)''')
    connect.commit()
    connect.close()

# Додає нову пару до бази даних.
def add_pair_to_db(user_id: int, day: str, time: str, name: str, link: str):
    connect = sqlite3.connect(DB_FILE)
    cursor = connect.cursor()
    cursor.execute("INSERT INTO schedule (user_id, day, time, name, link) VALUES (?, ?, ?, ?, ?)",
                   (user_id, day, time, name, link))
    connect.commit()
    connect.close()

# Отримує всі пари з БД для конкретного користувача та дня.
def get_pairs_for_day(user_id: int, day: str):
    connect = sqlite3.connect(DB_FILE)
    connect.row_factory = sqlite3.Row
    cursor = connect.cursor()
    cursor.execute("SELECT * FROM schedule WHERE user_id=? AND day=? ORDER BY time ASC", (user_id, day.lower())
                   )
    rows = cursor.fetchall()

    connect.close()
    return rows

# Отримує абсолютно всі пари для конкретного користувача.
def get_all_pairs(user_id: int):
    connect = sqlite3.connect(DB_FILE)
    connect.row_factory = sqlite3.Row
    cursor = connect.cursor()
    cursor.execute("SELECT * FROM schedule WHERE user_id=? ORDER BY day, time ASC", (user_id, ))
    rows = cursor.fetchall()
    connect.close()
    return rows

# Видаляє пару з бази даних за її ID.
def delete_pair_from_db(pair_id : int, user_id: int):
    connect = sqlite3.connect(DB_FILE)
    cursor = connect.cursor()
    cursor.execute("DELETE FROM schedule WHERE id=? AND user_id = ?", (pair_id,user_id))
    changes = cursor.rowcount
    connect.commit()
    connect.close()
    return changes > 0

# Додає нового користувача до БД, якщо він відсутній.
def add_user_if_not_exists(user_id : int, username : str):
    connect = sqlite3.connect(DB_FILE)
    cursor = connect.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, username, subscribed) VALUES (?, ?, 1)", (user_id, username))
    connect.commit()
    connect.close()

# Оновлює статус підписки користувача (1 - підписаний, 0 - ні).
def set_user_subscription(user_id : int, subscribed: int):
    connect = sqlite3.connect(DB_FILE)
    cursor = connect.cursor()
    cursor.execute("UPDATE users SET subscribed = ? WHERE user_id = ?", (subscribed, user_id))
    connect.commit()
    connect.close()

# Отримує список ID всіх користувачів, які підписані на розсилку.
def get_all_subscribed_users():
    connect = sqlite3.connect(DB_FILE)
    cursor = connect.cursor()
    cursor.execute("SELECT user_id FROM users WHERE subscribed = 1")
    user_ids = [row[0] for row in cursor.fetchall()]
    connect.close()
    return user_ids

# Обробник команди /start. Вітає користувача та реєструє його.
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    add_user_if_not_exists(user.id, user.username)
    text = (
        f"Привіт {user.first_name}!\n\n"
        "Я бот з розкладом. Я надсилатиму повідомлення про пари за 10 хвилин.\n\n"
        "**Команди:**\n"
        "/all - Показати весь розклад\n"
        "/subscribe - Увімкнути сповіщення (за замовчуванням)\n"
        "/unsubscribe - Вимкнути повідомлення\n"
    )
    if user.id == ADMIN_ID:
        text += ("\n**Панель адміну:**\n"
            "/add `[день] [час] [назва] [посилання(опціонально)]`\n"
            "/del `[номер]`")
    await update.message.reply_text(text, parse_mode="Markdown")

# Обробник команди /help. Повертає довідку по всім командам.
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

# Обробник команди /subscribe. Вмикає сповіщення.
async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_user_subscription(update.message.chat_id,1)
    await update.message.reply_text("✅ Повідомлення включено!")

# Обробник команди /unsubscribe. Вимикає сповіщення.
async def unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_user_subscription(update.message.chat_id,0)
    await update.message.reply_text("❌ Повідомлення вимкнено.")

# (Адмін) Обробник команди /add. Додає пару до розкладу.
async def add_para_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.chat_id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ Це команда тільки для адміністратора.")
        return

    if len(context.args) < 3:
        await update.message.reply_text("Формат: `/add [день] [время] [название] [ссылка]`", parse_mode='Markdown')
        return

    day, time, name = context.args[0], context.args[1], context.args[2]
    link = None
    if len(context.args) >= 4:
        link = context.args[3]
    try:
        add_pair_to_db(ADMIN_ID, day, time, name, link)
        await update.message.reply_text(f"✅ Додав пару до *загальний* розклад.")

    except Exception as e:
        await update.message.reply_text(f"❌ Помилка: {e}")

# Обробник команди /all. Повертає весь розклад.
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

        prefix = ""
        if user_id == ADMIN_ID:
            prefix = f"`[ID: {para['id']}]` "

        message += (
            f"{prefix}{day_counter}. `{para['time']}` - {para['name']}\n"
        )
        if para['link']:
             message += f" [Посилання]({para['link']})\n"

        day_counter += 1

    await update.message.reply_text(message, parse_mode="Markdown", disable_web_page_preview=True)

# Обробник команди /today. Повертає розклад на поточний день.
async def show_today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    try:
        current_day = datetime.now().strftime("%A").lower()
    except Exception as e:
        print(f"Помилка локалі: {e}")
        await update.message.reply_text(
            f"Помилка визначення дня: {e}. Перевірте налаштування локалі 'uk_UA.UTF-8' на сервері.")
        return

    pairs_today = get_pairs_for_day(ADMIN_ID, current_day)
    if not pairs_today:
        await update.message.reply_text(f"Сьогодні ({current_day.capitalize()}) пар немає. Відпочивайте! 🥳")
        return

    message = f"📅 **Розклад на сьогодні ({current_day.capitalize()}):**\n\n"

    for i, para in enumerate(pairs_today):
        prefix = ""
        if user_id == ADMIN_ID:
            prefix = f"`[ID: {para['id']}]` "

        message += (
            f"{prefix}{i + 1}. `{para['time']}` - {para['name']}\n"
        )
        if para['link']:
            message += f" [Посилання]({para['link']})\n"

    await update.message.reply_text(message, parse_mode="Markdown", disable_web_page_preview=True)

# (Адмін) Обробник команди /del. Видаляє пару за ID.
async def del_para_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.chat_id
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

already_notified = {}
# (Планувальник) Перевіряє розклад та розсилає нагадування.
async def check_schedule_and_broadcast(context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot
    now = datetime.now()
    current_day = now.strftime("%A").lower()
    current_time = now.strftime("%H:%M")

    print(f"[Розсилання] Перевірка... {current_day} {current_time}")

    pairs_today = get_pairs_for_day(ADMIN_ID, current_day)

    if not pairs_today:
        return

    for para in pairs_today:
        para_time_str = para['time']
        para_time = datetime.strptime(para_time_str, "%H:%M").time()

        remind_time = (datetime.combine(now.date(), para_time)- timedelta(minutes=REMIND_BEFORE_MINUTES)).time()
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
                            chat_id=user_id,
                            text=message,
                            parse_mode="Markdown",
                        )
                    except Exception as e:
                        print(f"[Розсилання] Помилка надсилання {user_id}: {e}. Відписую його.")
                        if "blocked" in str(e) or "deactivated" in str(e):
                            set_user_subscription(user_id,0)

                already_notified[notification_key] = True
        if current_time > para_time.strftime('%H:%M') and notification_key in already_notified:
            del already_notified[notification_key]

# Головна функція. Налаштовує та запускає бота.
def main():
    print("Ініціалізація бази даних (schedule + users)...")
    init_db()
    try:
        locale.setlocale(locale.LC_ALL, "uk_UA.UTF-8")
    except locale.Error:
        print("ПОПЕРЕДЖЕННЯ: Локаль 'uk_UA.UTF-8' не встановлена на сервері. Дні тижня можуть бути англійською.")
    print("Створення Application...")
    app = Application.builder().token(BOT_TOKEN).build()

    job_queue = app.job_queue
    job_queue.run_repeating(
        check_schedule_and_broadcast,
        interval=60,
        first=10
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("subscribe", subscribe_command))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe_command))
    app.add_handler(CommandHandler("all", show_all_command))
    app.add_handler(CommandHandler("today", show_today_command))
    app.add_handler(CommandHandler("day", show_today_command))
    app.add_handler(CommandHandler("add", add_para_command))
    app.add_handler(CommandHandler("del", del_para_command))

    print("Бот запущено в режимі (Адмін + Передплатники).")
    app.run_polling()


if __name__ == '__main__':
    main()