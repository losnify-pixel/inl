import logging
import uuid
import json
import asyncio
import os
import psycopg2
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# --- КОНФИГУРАЦИЯ ---

# 👇👇👇 ВСТАВЬ ТОКЕН СЮДА 👇👇👇
TOKEN = "8226690823:AAHUbV12-_AM2trJlh8ZHCglmJ4VLcGYRKQ"

# Scalingo сам заполнит это, когда подключишь базу
DATABASE_URL = os.environ.get("DATABASE_URL")

# --- ЛОГИРОВАНИЕ ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- СОСТОЯНИЯ FSM ---
(
    CHOOSING_ACTION,
    BUTTON_TYPE,
    BUTTON_TEXT,
    BUTTON_CONTENT,
    POLL_QUESTION,
    POLL_OPTIONS,
) = range(6)

TYPE_URL = "type_url"
TYPE_ALERT = "type_alert"

# --- РАБОТА С БАЗОЙ ДАННЫХ (Синхронная обертка) ---

def run_sql(sql, params=None, fetch=False):
    """Выполняет SQL в отдельном потоке, чтобы не тормозить бота."""
    if not DATABASE_URL:
        print("ОШИБКА: Нет подключения к БД (DATABASE_URL)")
        return None

    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            if fetch:
                result = cur.fetchall()
                return result
            conn.commit()
    except Exception as e:
        logger.error(f"SQL Error: {e}")
    finally:
        if conn:
            conn.close()

async def async_sql(sql, params=None, fetch=False):
    """Асинхронная обертка для SQL."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, run_sql, sql, params, fetch)

async def init_db():
    """Создание таблиц."""
    # Таблицы
    await async_sql("""
        CREATE TABLE IF NOT EXISTS polls (
            poll_id TEXT PRIMARY KEY,
            question TEXT,
            options TEXT
        )
    """)
    await async_sql("""
        CREATE TABLE IF NOT EXISTS votes (
            user_id BIGINT,
            poll_id TEXT,
            option_index INTEGER,
            PRIMARY KEY (user_id, poll_id)
        )
    """)
    await async_sql("""
        CREATE TABLE IF NOT EXISTS alerts (
            alert_id TEXT PRIMARY KEY,
            text TEXT
        )
    """)

# --- ЛОГИКА БОТА ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [
        [InlineKeyboardButton("➕ Создать кнопку", callback_data="create_btn")],
        [InlineKeyboardButton("📊 Создать опрос", callback_data="create_poll")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "Привет! Я бот для создания inline кнопок! Alex Doe на связи."
    
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    return CHOOSING_ACTION

async def action_create_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("🔗 URL-кнопка", callback_data=TYPE_URL)],
        [InlineKeyboardButton("💬 Кнопка с сообщением", callback_data=TYPE_ALERT)]
    ]
    await query.edit_message_text("Выбери тип кнопки:", reply_markup=InlineKeyboardMarkup(keyboard))
    return BUTTON_TYPE

async def button_type_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['btn_type'] = query.data
    await query.edit_message_text("Напиши текст для кнопки (макс. 64 символа):")
    return BUTTON_TEXT

async def button_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if len(text) > 64:
        await update.message.reply_text("Текст слишком длинный (макс 64). Попробуй еще раз:")
        return BUTTON_TEXT
    context.user_data['btn_text'] = text
    
    if context.user_data['btn_type'] == TYPE_URL:
        await update.message.reply_text("Отправь ссылку (URL):")
    else:
        await update.message.reply_text("Напиши текст всплывающего окна:")
    return BUTTON_CONTENT

async def button_content_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    content = update.message.text
    btn_type = context.user_data['btn_type']
    btn_text = context.user_data['btn_text']
    keyboard = []
    
    if btn_type == TYPE_URL:
        if not content.startswith(('http://', 'https://')):
            await update.message.reply_text("Ссылка должна начинаться с http:// или https://. Еще раз:")
            return BUTTON_CONTENT
        keyboard = [[InlineKeyboardButton(btn_text, url=content)]]
    else:
        alert_id = str(uuid.uuid4())[:8]
        await async_sql("INSERT INTO alerts (alert_id, text) VALUES (%s, %s)", (alert_id, content))
        
        callback_data = f"alert:{alert_id}"
        keyboard = [[InlineKeyboardButton(btn_text, callback_data=callback_data)]]

    await update.message.reply_text(
        "Твоя кнопка готова! 👇\nПерешли сообщение в нужный канал.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    context.user_data.clear()
    return ConversationHandler.END

# --- ОПРОСЫ ---

async def action_create_poll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Напиши текст вопроса:")
    return POLL_QUESTION

async def poll_question_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['poll_question'] = update.message.text
    await update.message.reply_text("Варианты ответов через запятую (макс 10):")
    return POLL_OPTIONS

async def poll_options_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw_options = update.message.text
    options = [opt.strip() for opt in raw_options.split(',') if opt.strip()]
    if not options or len(options) > 10:
        await update.message.reply_text(f"Получено {len(options)} вариантов. Нужно 1-10. Еще раз:")
        return POLL_OPTIONS
    
    question = context.user_data['poll_question']
    poll_id = str(uuid.uuid4())
    
    await async_sql(
        "INSERT INTO polls (poll_id, question, options) VALUES (%s, %s, %s)",
        (poll_id, question, json.dumps(options))
    )
    
    keyboard = generate_poll_keyboard(poll_id, options, {})
    await update.message.reply_text(
        f"📊 <b>{question}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    context.user_data.clear()
    return ConversationHandler.END

def generate_poll_keyboard(poll_id: str, options: list, votes_summary: dict) -> list:
    keyboard = []
    for idx, text in enumerate(options):
        count = votes_summary.get(idx, 0)
        btn_text = f"{text} ({count})" if count > 0 else text
        callback_data = f"vote:{poll_id}:{idx}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback_data)])
    return keyboard

# --- HANDLERS ---

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    
    if data.startswith("alert:"):
        alert_id = data.split(":")[1]
        text = "Ошибка."
        rows = await async_sql("SELECT text FROM alerts WHERE alert_id = %s", (alert_id,), fetch=True)
        if rows:
            text = rows[0][0]
        await query.answer(text, show_alert=True)
        return

    if data.startswith("vote:"):
        _, poll_id, option_idx = data.split(":")
        option_idx = int(option_idx)
        user_id = query.from_user.id
        
        # 1. Берем опции
        rows = await async_sql("SELECT options FROM polls WHERE poll_id = %s", (poll_id,), fetch=True)
        if not rows:
            await query.answer("Опрос удален.", show_alert=True)
            return
        options = json.loads(rows[0][0])

        # 2. Проверка голоса
        vote_rows = await async_sql("SELECT option_index FROM votes WHERE user_id = %s AND poll_id = %s", (user_id, poll_id), fetch=True)
        
        if vote_rows:
            if vote_rows[0][0] == option_idx:
                await query.answer("Уже выбрано!")
                return
            await async_sql("UPDATE votes SET option_index = %s WHERE user_id = %s AND poll_id = %s", (option_idx, user_id, poll_id))
        else:
            await async_sql("INSERT INTO votes (user_id, poll_id, option_index) VALUES (%s, %s, %s)", (user_id, poll_id, option_idx))
        
        # 3. Подсчет
        results = await async_sql("SELECT option_index, COUNT(*) FROM votes WHERE poll_id = %s GROUP BY option_index", (poll_id,), fetch=True)
        votes_summary = {row[0]: row[1] for row in results}
            
        try:
            new_kb = generate_poll_keyboard(poll_id, options, votes_summary)
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_kb))
            await query.answer("Голос принят")
        except Exception:
            await query.answer()

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
    context.user_data.clear()
    return ConversationHandler.END

# --- STARTUP ---

def main():
    if not DATABASE_URL:
        print("ОШИБКА: Не задан DATABASE_URL. В Scalingo: вкладка Addons -> добавь PostgreSQL.")
        # Не выходим, чтобы видеть логи
        
    application = Application.builder().token(TOKEN).build()

    # Инициализация БД
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(init_db())
    except Exception as e:
        print(f"Ошибка старта БД: {e}")

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(action_create_button, pattern="^create_btn$"),
            CallbackQueryHandler(action_create_poll, pattern="^create_poll$")
        ],
        states={
            CHOOSING_ACTION: [CallbackQueryHandler(action_create_button, pattern="^create_btn$"), CallbackQueryHandler(action_create_poll, pattern="^create_poll$")],
            BUTTON_TYPE: [CallbackQueryHandler(button_type_chosen, pattern=f"^({TYPE_URL}|{TYPE_ALERT})$")],
            BUTTON_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, button_text_received)],
            BUTTON_CONTENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, button_content_received)],
            POLL_QUESTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, poll_question_received)],
            POLL_OPTIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, poll_options_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(handle_callback_query, pattern="^(vote:|alert:)"))

    print("Бот запускается (psycopg2-binary version)...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
