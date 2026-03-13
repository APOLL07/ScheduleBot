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

REMIND_BEFORE_MINUTES = 10
TIMEZONE = pytz.timezone('Europe/Kiev')

REFERENCE_DATE = datetime(2025, 2, 24).date()
REFERENCE_WEEK_TYPE = "парний"

DAY_OF_WEEK_UKR = {0: "понеділок", 1: "вівторок", 2: "середа", 3: "четвер", 4: "п'ятниця", 5: "субота", 6: "неділя"}
DAY_ORDER_LIST = ["понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"]
AI_TO_DB_DAYS = {"Monday": "понеділок", "Tuesday": "вівторок", "Wednesday": "середа", "Thursday": "четвер", "Friday": "п'ятниця", "Saturday": "субота", "Sunday": "неділя"}
AI_TO_DB_WEEKS = {"odd": "непарна", "even": "парна", "both": "кожна"}

# РОЗКЛАД ДЗВІНКІВ ЗГІДНО З ФОТО
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
# БАЗА ДАНИХ
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
            conn.commit()
    except Exception as e:
        print(f"ПОМИЛКА init_db: {e}")

def add_pair_to_db(user_id: int, day: str, time_str: str, name: str, link: str, week_type: str, pair_order: int = 0):
    sql = "INSERT INTO schedule (user_id, day, time, name, link, week_type, pair_order) VALUES (%s, %s, %s, %s, %s, %s, %s)"
    with get_db_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (user_id, day.lower(), time_str, name, link, week_type, pair_order))
        conn.commit()

def delete_specific_pair(user_id: int, day: str, pair_order: int, week_type: str):
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

        data = item.get("data", {})
        if not action or not data: continue

        day_eng = data.get("day")
        week_eng = data.get("week", "both") 
        order = data.get("order")
        subject = data.get("subject", "Без назви")
        link = data.get("link", "None")
        custom_time = data.get("custom_time") # ДОДАНО ДЛЯ ТЕСТОВИХ ПАР

        if not day_eng or not order: continue

        day_ukr = AI_TO_DB_DAYS.get(day_eng, "понеділок")
        week_ukr = AI_TO_DB_WEEKS.get(week_eng, "кожна")
        
        # ЛОГІКА КАСТОМНОГО ЧАСУ
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
             FROM schedule WHERE user_id=%s AND day =%s AND (week_type='кожна' OR week_type=%s)
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

def get_schedule_for_current_week(user_id: int, start_of_week_date: datetime.date):
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
    except Exception as e:
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
                                    
                            msg = f"🔔 **Нагадування!**\n\nЧерез {REMIND_BEFORE_MINUTES} хвилин ({pair['time']}) почнеться пара:\n**{pair['name']}**{link_msg}"
                            await bot.send_message(user_id, msg, parse_mode="Markdown", disable_web_page_preview=True)
                            mark_as_notified(notification_key)
                except Exception as e:
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
    if user_id != ADMIN_ID: return 
    if not ai_client: return await update.message.reply_text("❌ Ключ Cohere не підключено.")

    text = update.message.text
    
    # --- ЗБИРАЄМО ГОТОВІ ШПАРГАЛКИ ДЛЯ ШІ ---
    now = datetime.now(TIMEZONE)
    current_time_str = now.strftime('%Y-%m-%d %H:%M:%S') # ВАЖЛИВО ДЛЯ ТЕСТІВ ЧАСУ
    
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
    
    pairs_even = [p for p in all_pairs if p['week_type'] in ['парна', 'кожна']]
    text_even = format_pairs_message(pairs_even, "Розклад на ПАРНИЙ тиждень:")
    
    pairs_odd = [p for p in all_pairs if p['week_type'] in ['непарна', 'кожна']]
    text_odd = format_pairs_message(pairs_odd, "Розклад на НЕПАРНИЙ тиждень:")

    system_prompt = f"""
    Ти — розумний персональний асистент Олега з розкладу (Одеська політехніка).
    
    --- ГОТОВІ ДАНІ ---
    ПОТОЧНИЙ ЧАС: {current_time_str}
    
    [РОЗКЛАД НА СЬОГОДНІ]:
    {text_today}
    
    [РОЗКЛАД НА ЗАВТРА]:
    {text_tomorrow}
    
    [ПОВНИЙ РОЗКЛАД ОЛЕГА]:
    {text_all}
    
    [СЛОВНИК ПРЕДМЕТІВ ОЛЕГА (АБСОЛЮТНА ІСТИНА)]:
    - "математика", "матан" = "Математичні основи захисту інформації - доц. Морозов Юрій Олександрович"
    - "прога", "програмування" = "Технологія програмування - доц. Ярова І.А." (або Головачова О.В.)
    - "іт", "інфа", "інформатика" = "Інформаційні технології - С.в. Вінковська Ірина Сергіївна"
    - "фізика" = "Спеціальні розділи фізики - Дедюра К.О."
    - "англійська", "інгліш", "іноземна" = "Іноземна мова - Єршова Ю.А." (або Воробйова К.В.)
    - "кібербезпека", "політики" = "Політики кібербезпеки та захисту інформації - доц. Мельник Г.М."
    - "філософія" = "Філософія - Афанасьєв О.І."
    ---------------------------------------------------
    
    ТВОЄ ЗАВДАННЯ:
    Проаналізуй запит Олега і поверни ВИКЛЮЧНО валідний JSON.
    
    Формат JSON (СУВОРО дотримуйся цієї структури):
    {{
      "reply": "Твоя відповідь українською...",
      "db_actions": [
        {{
          "action": "UPDATE", 
          "data": {{
            "day": "Friday", 
            "week": "even", 
            "order": 1, 
            "subject": "Повна офіційна назва...", 
            "link": "https://...",
            "custom_time": "14:15" 
          }}
        }}
      ]
    }}
    
    ДОСТУПНІ ДІЇ (action): "UPDATE", "DELETE", "DELETE_ALL".
    day: "Monday", "Tuesday", "Wednesday", "Thursday", "Friday".
    week: "odd" (непарний), "even" (парний), "both" (кожен тиждень).
    order: 1, 2, 3, 4 або 5 (або 99 для тестових пар).
    
    ПРАВИЛА БАЗИ ДАНИХ - КРИТИЧНО ВАЖЛИВО:
    1. ДІЙ БЕЗ ЗАПИТАНЬ: Якщо Олег каже "зміни", "виправ", "додай" — МИТТЄВО генеруй дію "UPDATE".
    2. АВТОМАТИЧНЕ ВИПРАВЛЕННЯ НА ОФІЦІЙНУ НАЗВУ: Звіряйся зі [СЛОВНИКА ПРЕДМЕТІВ]. Записуй тільки ПОВНУ назву з викладачем.
    3. ЗБЕРЕЖЕННЯ ТИЖНІВ: Дивись в [ПОВНИЙ РОЗКЛАД ОЛЕГА], який там стояв тиждень для цієї пари, і записуй його в поле "week".
    4. ЗБЕРЕЖЕННЯ ДАНИХ ПІДКЛЮЧЕННЯ: В поле "link" записуй ВСЕ: URL, Meeting ID, Passcode. Якщо каже "старе посилання", копіюй його з бази.
    5. НЕСТАНДАРТНИЙ ЧАС ТА ТЕСТИ (ДЛЯ НАГАДУВАНЬ): Якщо Олег просить додати тестову пару "через 10 хвилин" або на конкретний час, подивись на ПОТОЧНИЙ ЧАС ({current_time_str}), математично обчисли точний час, коли буде пара. У JSON передай "order": 99, та обов'язково додай поле "custom_time" у форматі "ЧЧ:ММ" (наприклад, "custom_time": "14:12").
    6. Якщо просить переписати все з нуля — спочатку додай дію "DELETE_ALL".
    """
    
    processing_msg = await update.message.reply_text("⏳ Виконую наказ...")
    
    try:
        response = await ai_client.chat(
            message=text,
            preamble=system_prompt,
            model="command-a-03-2025",
            temperature=0.1 
        )
        
        raw_text = response.text.strip()
        match = re.search(r'\{.*\}', raw_text, re.DOTALL)
        if match:
            clean_json = match.group(0)
        else:
            clean_json = raw_text.replace("```json", "").replace("```", "").strip()
            
        ai_json = json.loads(clean_json)
        
        reply_text = ai_json.get("reply", "Зрозумів!")
        db_actions = ai_json.get("db_actions", [])
        
        changes_count = execute_db_actions(ADMIN_ID, db_actions)
        
        final_message = reply_text
        if changes_count > 0:
            final_message += f"\n\n⚙️ _Виконано дій з базою: {changes_count}_"
            
        await processing_msg.edit_text(final_message, parse_mode="Markdown", disable_web_page_preview=True)

    except json.JSONDecodeError:
        await processing_msg.edit_text("❌ Помилка: ШІ знову порушив формат JSON.\nВідповідь ШІ:\n" + raw_text)
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
    text = f"Привіт!\nЯ твій розумний AI-асистент з розкладом.\n\n/all - Розклад на тиждень\n/today - На сьогодні\n/manage - Управління\nАбо просто запитай мене: 'Яка в мене завтра перша пара?'"
    await update.message.reply_text(text, parse_mode="Markdown")

async def manage_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
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
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ai_text_handler))

    await application.initialize()
    main_loop = asyncio.get_running_loop()
    
    if WEBHOOK_URL:
        await application.bot.set_webhook(f"{WEBHOOK_URL}/webhook/{BOT_TOKEN}", allowed_updates=Update.ALL_TYPES)

    init_db()
    
    # Запуск планувальника нагадувань (щохвилини)
    scheduler.init_app(flask_app)
    scheduler.add_job(id='RemindersJob', func=scheduled_job_wrapper, trigger='interval', minutes=1)
    scheduler.start()
    
    yield

app = Flask(__name__)
flask_app = app 

@app.route('/')
def health_check(): return "OK", 200

@app.route(f'/webhook/{BOT_TOKEN}', methods=['POST'])
async def webhook():
    update = Update.de_json(flask_request.get_json(), application.bot)
    await application.process_update(update)
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