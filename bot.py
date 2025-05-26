import logging
import os
import asyncio
import pandas as pd
import time
from collections import defaultdict
from functools import lru_cache
from datetime import datetime, timezone
from pydantic_settings import BaseSettings
from pydantic import Field
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler
)
from flask import Flask, request, jsonify
from hypercorn.asyncio import serve
from hypercorn.config import Config

# --- CONFIGURATION ---
class Settings(BaseSettings):
    BOT_TOKEN: str
    WEBHOOK_URL: str
    PORT: int = Field(default=int(os.getenv('PORT', 8443)))
    EXCEL_FILE_PATH: str = Field(default="rep.xlsx")
    MAX_REQUESTS_PER_MINUTE: int = Field(default=30)
    CACHE_TTL: int = Field(default=300)
    WEBHOOK_PATH: str = Field(default="/webhook_telegram")
    DEBUG: bool = Field(default=False)
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
settings = Settings()
CHANNEL_ID = -1002647531334

MESSAGES = {
    "welcome": (
        "三上はじめにようこそお越しくださいました。ご利用いただき、誠にありがとうございます。\n"
        "下記の選択肢よりご希望の項目をお選びください。\n\n"
        "※ボタンを押した後、処理に数秒かかる場合がございます。反応がない場合は再度お試しください。"
    ),
    "processing": "⏳ 現在処理中です。しばらくお待ちください。",
    "next_step": "次の項目をお選びください。",
    "selected": "ご選択いただいた項目：{}",
    "no_data": "申し訳ございませんが、現在ご案内可能なデータがございません。",
    "rate_limit": "リクエストが多すぎます。しばらく経ってから再度お試しください。",
    "error": "エラーが発生しました。お手数ですが、もう一度お試しください。",
    "ask_time": (
        "お客様の番号：<b>{}</b>\n\n"
        "ご到着予定時刻をお知らせください。（下記より選択、または「その他」の場合はご入力ください）"
    ),
    "ask_manual_time": "ご到着予定時刻を「HH:MM」形式でご入力くださいませ。",
    "final_thanks": (
        "ご入力いただき、誠にありがとうございます。\n"
        "お客様のご到着を心よりお待ち申し上げております。\n"
        "ご不明点がございましたらお気軽にご連絡くださいませ。"
    )
}

# --- STATE ---
class State:
    def __init__(self):
        self.data = []
        self.string_ids = {}
        self.id_strings = {}
        self.next_id = 0
        self.last_refresh = 0
        self._requests = defaultdict(list)
        self.processing = {}
        self.user_message_ids = defaultdict(list)
        self.waiting_time_input = {}

    def can_request(self, user_id: int) -> bool:
        now = time.time()
        req = self._requests[user_id] = [r for r in self._requests[user_id] if now - r < 60]
        if len(req) >= settings.MAX_REQUESTS_PER_MINUTE: return False
        req.append(now)
        return True

    def get_id(self, s: str) -> int:
        if not s: return -1
        if s not in self.string_ids:
            self.string_ids[s] = self.next_id
            self.id_strings[self.next_id] = s
            self.next_id += 1
        return self.string_ids[s]
    def get_string(self, i: int) -> str:
        return self.id_strings.get(i, '')

state = State()
logger = logging.getLogger(__name__)
flask_app = Flask(__name__)
application = None

# --- DATA LOADING ---
@lru_cache(maxsize=1)
def load_excel_data():
    try:
        df = pd.read_excel(settings.EXCEL_FILE_PATH, engine='openpyxl', na_values=[''])
        df = df.fillna('')
        return df.astype(str).to_dict(orient='records')
    except Exception as e:
        logger.error(f"Excel loading error: {e}")
        return []

def refresh_data():
    now = time.time()
    if now - state.last_refresh > settings.CACHE_TTL:
        load_excel_data.cache_clear()
        data = load_excel_data()
        if data:
            state.data = data
            state.last_refresh = now
            for row in data:
                for field in ["Key", "Rep1", "Rep2"]:
                    if row[field]: state.get_id(row[field])

def get_display_name(user):
    # Ưu tiên tên đầy đủ, nếu không có thì dùng username, nếu không có thì dùng id
    return (user.full_name or user.username or str(user.id)).strip()

# --- BOT UTILITIES ---
async def safe_send_and_track(func, update, *args, **kwargs):
    try:
        sent_msg = await func(*args, **kwargs)
        if sent_msg and hasattr(sent_msg, 'message_id'):
            user_id = update.effective_user.id
            state.user_message_ids[user_id].append(sent_msg.message_id)
        return sent_msg
    except Exception as e:
        logger.warning(f"Send error: {e}")

async def delete_user_messages(update, context):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    for msg_id in state.user_message_ids[user_id]:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
    state.user_message_ids[user_id].clear()

# --- BOT HANDLERS ---
async def send_initial_buttons(update: Update, context=None):
    refresh_data()
    if not state.data:
        await safe_send_and_track(update.message.reply_text, update, MESSAGES["no_data"])
        return
    keys = sorted({row["Key"] for row in state.data if row["Key"]})
    keyboard = [[InlineKeyboardButton(k, callback_data=f"key:{state.get_id(k)}::")] for k in keys]
    await safe_send_and_track(
        update.message.reply_text,
        update,
        MESSAGES["welcome"],
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not state.can_request(user_id):
        await safe_send_and_track(update.message.reply_text, update, MESSAGES["rate_limit"])
        return
    state.processing[user_id] = False
    state.waiting_time_input.pop(user_id, None)
    await delete_user_messages(update, context)
    await send_initial_buttons(update, context)

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    user_id = user.id
    if not state.can_request(user_id) or state.processing.get(user_id):
        await safe_send_and_track(query.answer, update, MESSAGES["processing"])
        return
    try:
        state.processing[user_id] = True
        await safe_send_and_track(query.answer, update)
        refresh_data()
        level, *ids = query.data.split(':')
        ids = [int(i) if i else -1 for i in ids]
        key_id, rep1_id, rep2_id = ids + [-1] * (3 - len(ids))
        key = state.get_string(key_id)
        rep1 = state.get_string(rep1_id) if rep1_id != -1 else ''
        rep2 = state.get_string(rep2_id) if rep2_id != -1 else ''
        if level == "key":
            rep1s = sorted({row["Rep1"] for row in state.data if row["Key"] == key and row["Rep1"]})
            if rep1s:
                keyboard = [[InlineKeyboardButton(r1, callback_data=f"rep1:{key_id}:{state.get_id(r1)}:")] for r1 in rep1s]
                await safe_send_and_track(query.edit_message_text, update, f"{MESSAGES['selected'].format(key)}\n{MESSAGES['next_step']}", reply_markup=InlineKeyboardMarkup(keyboard))
        elif level == "rep1":
            rep2s = sorted({row["Rep2"] for row in state.data if row["Key"] == key and row["Rep1"] == rep1 and row["Rep2"]})
            if rep2s:
                keyboard = [[InlineKeyboardButton(r2, callback_data=f"rep2:{key_id}:{rep1_id}:{state.get_id(r2)}")] for r2 in rep2s]
                await safe_send_and_track(query.edit_message_text, update, f"{MESSAGES['selected'].format(rep1)}\n{MESSAGES['next_step']}", reply_markup=InlineKeyboardMarkup(keyboard))
        elif level == "rep2":
            row = next((row for row in state.data if row["Key"] == key and row["Rep1"] == rep1 and row["Rep2"] == rep2), None)
            if row:
                rep3 = row.get("Rep3", "")
                rep4 = row.get("Rep4", "")
            else:
                rep3, rep4 = MESSAGES["no_data"], ""
            state.waiting_time_input[user_id] = {
                'rep3': rep3,
                'rep4': rep4,
                'name': get_display_name(user),
                'user_id': user.id
            }
            times = [
                ("08:00", "08:00"), ("09:00", "09:00"), ("10:00", "10:00"),
                ("12:00", "12:00"), ("14:00", "14:00"), ("16:00", "16:00"),
                ("18:00", "18:00"), ("20:00", "20:00"), ("その他", "other")
            ]
            keyboard = [[InlineKeyboardButton(label, callback_data=f"time:{t}")] for label, t in times]
            await safe_send_and_track(
                query.edit_message_text, update,
                MESSAGES["ask_time"].format(rep3),
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='HTML'
            )
    except Exception as e:
        logger.error(f"Button handler error: {e}")
        await safe_send_and_track(query.message.reply_text, update, MESSAGES["error"])
    finally:
        state.processing[user_id] = False

async def handle_time_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    data = query.data or ""
    if not data.startswith("time:"):
        return
    time_value = data[5:]
    await safe_send_and_track(query.answer, update)
    if user_id not in state.waiting_time_input:
        await safe_send_and_track(
            query.edit_message_text, update,
            "再度最初からご選択ください。"
        )
        return
    if time_value == "other":
        try:
            await context.bot.delete_message(chat_id=query.message.chat.id, message_id=query.message.message_id)
        except Exception:
            pass
        await safe_send_and_track(
            update.effective_chat.send_message, update,
            MESSAGES["ask_manual_time"],
            reply_markup=ForceReply(selective=True)
        )
        state.waiting_time_input[user_id]["waiting_manual_time"] = True
    else:
        await send_to_channel_and_finish(update, context, user_id, time_value)

async def handle_manual_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if (
        user_id in state.waiting_time_input and 
        state.waiting_time_input[user_id].get("waiting_manual_time")
    ):
        time_value = update.message.text.strip()
        await send_to_channel_and_finish(update, context, user_id, time_value)
        state.waiting_time_input[user_id].pop("waiting_manual_time", None)

async def send_to_channel_and_finish(update, context, user_id, time_value):
    info = state.waiting_time_input.pop(user_id, None)
    if not info:
        logger.warning(f"No waiting_time_input for user {user_id}")
        return
    # --- Cấu trúc mới: Mã số - Số hiệu - Tên KH @user_id - Thời gian đến ---
    msg = f"{info['rep3']} - {info['rep4']} - {info['name']} @{info['user_id']} - {time_value}"
    await context.bot.send_message(chat_id=CHANNEL_ID, text=msg)
    await safe_send_and_track(update.effective_message.reply_text, update, MESSAGES["final_thanks"], parse_mode='HTML')

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling update:", exc_info=context.error)

# --- APP SETUP ---
@flask_app.route(settings.WEBHOOK_PATH, methods=["POST"])
async def webhook_handler():
    if not application:
        return "Bot not ready", 503
    try:
        data = request.get_json(force=True)
        if data:
            await process_update(data)
        return "ok", 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "ok", 200

async def process_update(update_dict: dict):
    update = Update.de_json(update_dict, application.bot)
    await application.process_update(update)

@flask_app.route("/health")
def health_check():
    return jsonify({
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "1.0.0"
    })

async def init_application():
    global application
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.DEBUG if settings.DEBUG else logging.INFO
    )
    try:
        application = (
            ApplicationBuilder()
            .token(settings.BOT_TOKEN)
            .concurrent_updates(True)
            .build()
        )
        application.add_handler(CommandHandler("start", handle_start))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_manual_time))
        application.add_handler(CallbackQueryHandler(handle_time_select, pattern="^time:"))
        application.add_handler(CallbackQueryHandler(handle_button))
        application.add_error_handler(error_handler)
        await application.initialize()
        await application.bot.set_webhook(url=f"{settings.WEBHOOK_URL}{settings.WEBHOOK_PATH}")
        refresh_data()
        return True
    except Exception as e:
        logger.critical(f"Initialization error: {e}")
        return False

async def run_application():
    try:
        if await init_application():
            config = Config()
            config.bind = [f"0.0.0.0:{settings.PORT}"]
            await serve(flask_app, config)
        else:
            raise RuntimeError("Application initialization failed")
    except Exception as e:
        logger.critical(f"Startup error: {e}")
        raise

if __name__ == '__main__':
    try:
        asyncio.run(run_application())
    except KeyboardInterrupt:
        logger.info("Shutdown by user")
    except Exception as e:
        logger.critical(f"Fatal error: {e}")
        raise
