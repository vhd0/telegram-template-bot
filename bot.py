import logging
import os
import asyncio
import pandas as pd
import time
from collections import defaultdict
from functools import lru_cache
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from flask import Flask, request, jsonify
from hypercorn.asyncio import serve
from hypercorn.config import Config

# --- Config ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8443))
EXCEL_FILE_PATH = os.getenv("EXCEL_FILE_PATH", "rep.xlsx")
CACHE_TTL = int(os.getenv("CACHE_TTL", 300))
WEBHOOK_PATH = "/webhook_telegram"
CHANNEL_ID = os.getenv("CHANNEL_ID", "-1002647531334")
MAX_REQUESTS_PER_MINUTE = 30

MESSAGES = {
    "welcome": (
        "三上はじめにようこそお越しくださいました。ご利用いただき、誠にありがとうございます。\n"
        "ご希望の場所を以下の選択肢よりお選びください。\n\n"
        "※ボタンを押した後、処理に数秒かかる場合がございます。反応がない場合は再度お試しください。"
    ),
    "processing": "⏳ 現在処理中です。しばらくお待ちください。",
    "next_step": "次の項目をお選びください。",
    "selected": "ご選択いただいた項目：{}",
    "no_data": "申し訳ございませんが、現在ご案内可能なデータがございません。",
    "rate_limit": "リクエストが多すぎます。しばらく経ってから再度お試しください。",
    "error": "エラーが発生しました。お手数ですが、もう一度お試しください。",
    "ask_time": "ご到着予定時刻をお知らせください。（下記より選択、または「その他」の場合はご入力ください）",
    "ask_manual_time": "ご到着予定時刻を「HH:MM」形式でご入力くださいませ。",
    "final_thanks": (
        "この度は、ご利用ありがとうございます。\n"
        "三上が内容を確認し、追ってご連絡差し上げます。\n"
        "弊社からのメッセージを見逃さないよう、お電話にご注意ください。"
    )
}

# --- Logging ---
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

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
        self._requests[user_id] = [r for r in self._requests[user_id] if now - r < 60]
        if len(self._requests[user_id]) >= MAX_REQUESTS_PER_MINUTE:
            return False
        self._requests[user_id].append(now)
        return True

    def get_id(self, s: str) -> int:
        if not s:
            return -1
        if s not in self.string_ids:
            self.string_ids[s] = self.next_id
            self.id_strings[self.next_id] = s
            self.next_id += 1
        return self.string_ids[s]

    def get_string(self, i: int) -> str:
        return self.id_strings.get(i, '')

state = State()
app = Flask(__name__)
application = None
main_loop = None  # GLOBAL event loop

# --- Data Management ---
@lru_cache(maxsize=1)
def load_excel_data():
    try:
        df = pd.read_excel(EXCEL_FILE_PATH, engine='openpyxl', na_values=[''])
        return df.fillna('').astype(str).to_dict(orient='records')
    except Exception as e:
        logger.error(f"Excel loading error: {e}")
        return []

def refresh_data():
    now = time.time()
    if now - state.last_refresh > CACHE_TTL:
        load_excel_data.cache_clear()
        if data := load_excel_data():
            state.data = data
            state.last_refresh = now
            for row in data:
                for field in ["Key", "Rep1", "Rep2"]:
                    if row[field]:
                        state.get_id(row[field])

# --- Message Handling ---
async def send_message(update, message_func, text, **kwargs):
    try:
        msg = await message_func(text=text, **kwargs)
        if msg and hasattr(msg, 'message_id'):
            state.user_message_ids[update.effective_user.id].append(msg.message_id)
        return msg
    except Exception as e:
        logger.error(f"Message error: {e}")
        return None

async def delete_messages(update, context):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    for msg_id in state.user_message_ids[user_id]:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
    state.user_message_ids[user_id].clear()

# --- Handlers ---
async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not state.can_request(user_id):
        return await send_message(update, update.message.reply_text, MESSAGES["rate_limit"])
    state.processing[user_id] = False
    state.waiting_time_input.pop(user_id, None)
    await delete_messages(update, context)
    refresh_data()
    if not state.data:
        return await send_message(update, update.message.reply_text, MESSAGES["no_data"])
    keys = sorted({row["Key"] for row in state.data if row["Key"]})
    keyboard = [[InlineKeyboardButton(k, callback_data=f"key:{state.get_id(k)}::")] for k in keys]
    await send_message(
        update,
        update.message.reply_text,
        MESSAGES["welcome"],
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    if not state.can_request(user_id) or state.processing.get(user_id):
        try:
            await query.answer(MESSAGES["processing"], show_alert=True)
        except Exception:
            pass
        return
    try:
        state.processing[user_id] = True
        try:
            await query.answer()
        except Exception:
            pass
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
                await query.edit_message_text(
                    text=f"{MESSAGES['selected'].format(key)}\n{MESSAGES['next_step']}",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            else:
                await query.edit_message_text(text=MESSAGES["no_data"])
        elif level == "rep1":
            rep2s = sorted({row["Rep2"] for row in state.data if row["Key"] == key and row["Rep1"] == rep1 and row["Rep2"]})
            if rep2s:
                keyboard = [[InlineKeyboardButton(r2, callback_data=f"rep2:{key_id}:{rep1_id}:{state.get_id(r2)}")] for r2 in rep2s]
                await query.edit_message_text(
                    text=f"{MESSAGES['selected'].format(rep1)}\n{MESSAGES['next_step']}",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            else:
                await query.edit_message_text(text=MESSAGES["no_data"])
        elif level == "rep2":
            row = next((row for row in state.data if row["Key"] == key and row["Rep1"] == rep1 and row["Rep2"] == rep2), None)
            if row:
                state.waiting_time_input[user_id] = {
                    'rep3': row.get("Rep3", ""),
                    'rep4': row.get("Rep4", ""),
                    'name': update.effective_user.full_name or update.effective_user.username or str(user_id),
                    'user_id': user_id,
                    'username': update.effective_user.username
                }
                times = [
                    ("11:00", "11:00"), ("12:00", "12:00"), ("13:00", "13:00"),
                    ("14:00", "14:00"), ("16:00", "16:00"), ("18:00", "18:00"),
                    ("20:00", "20:00"), ("22:00", "22:00"), ("その他", "other")
                ]
                keyboard = [[InlineKeyboardButton(label, callback_data=f"time:{t}")] for label, t in times]
                await query.edit_message_text(
                    text=MESSAGES["ask_time"],
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='HTML'
                )
            else:
                await query.edit_message_text(text=MESSAGES["no_data"])
        else:
            await query.edit_message_text(text=MESSAGES["error"])
    except Exception as e:
        logger.error(f"Button handler error: {e}")
        try:
            await query.answer(MESSAGES["error"], show_alert=True)
        except Exception:
            pass
        try:
            await send_message(update, query.message.reply_text, MESSAGES["error"])
        except Exception:
            pass
    finally:
        state.processing[user_id] = False

async def handle_time_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    if not (data := query.data) or not data.startswith("time:"):
        return
    time_value = data[5:]
    try:
        await query.answer()
    except Exception:
        pass
    if user_id not in state.waiting_time_input:
        try:
            await query.edit_message_text(text="再度最初からご選択ください。")
        except Exception:
            pass
        return
    if time_value == "other":
        try:
            await context.bot.delete_message(
                chat_id=query.message.chat.id,
                message_id=query.message.message_id
            )
        except Exception:
            pass
        await send_message(
            update,
            update.effective_chat.send_message,
            MESSAGES["ask_manual_time"],
            reply_markup=ForceReply(selective=True)
        )
        state.waiting_time_input[user_id]["waiting_manual_time"] = True
    else:
        await send_to_channel_and_finish(update, context, user_id, time_value)

async def handle_manual_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in state.waiting_time_input and state.waiting_time_input[user_id].get("waiting_manual_time"):
        await send_to_channel_and_finish(update, context, user_id, update.message.text.strip())
        state.waiting_time_input[user_id].pop("waiting_manual_time", None)

async def send_to_channel_and_finish(update, context, user_id, time_value):
    if not (info := state.waiting_time_input.pop(user_id, None)):
        logger.warning(f"No waiting_time_input for user {user_id}")
        return
    username = info.get('username')
    kh_info = (
        f'<a href="https://t.me/{username}">{info["name"]} (@{username})</a>'
        if username else
        f'<a href="tg://user?id={info["user_id"]}">{info["name"]}</a>'
    )
    msg = f'{info["rep3"]} - {info["rep4"]} - {kh_info} - {time_value}'
    try:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=msg,
            parse_mode='HTML'
        )
    except Exception as e:
        logger.error(f"Channel message error: {e}")

    # XÓA TẤT CẢ TIN NHẮN TRƯỚC ĐÓ
    await delete_messages(update, context)

    # GỬI LẠI CHỈ TIN NHẮN FINAL
    await send_message(
        update,
        update.effective_chat.send_message,  # CHÚ Ý: dùng send_message thay vì reply_text để không bị reply vào tin cũ vừa xóa
        MESSAGES["final_thanks"],
        parse_mode='HTML'
    )
    except Exception as e:
        logger.error(f"Channel message error: {e}")
    await send_message(
        update,
        update.effective_message.reply_text,
        MESSAGES["final_thanks"],
        parse_mode='HTML'
    )

# --- Webhook ---
@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook_handler():
    global main_loop
    if not application or not main_loop:
        return "Bot not ready", 503
    try:
        if data := request.get_json(force=True):
            update = Update.de_json(data, application.bot)
            # Đảm bảo dùng đúng main_loop (loop khởi tạo bot), không tạo loop mới!
            future = asyncio.run_coroutine_threadsafe(application.process_update(update), main_loop)
            try:
                future.result(timeout=10)  # Có thể bỏ timeout nếu muốn fire-and-forget
            except Exception as err:
                logger.error(f"Coroutine error: {err}")
        return "ok", 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "ok", 200

@app.route("/health")
def health_check():
    return jsonify({
        "status": "ok",
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0.0"
    })

# --- Init ---
async def init_bot():
    global application, main_loop
    try:
        application = (
            ApplicationBuilder()
            .token(BOT_TOKEN)
            .concurrent_updates(True)
            .build()
        )
        application.add_handler(CommandHandler("start", handle_start))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_manual_time))
        application.add_handler(CallbackQueryHandler(handle_time_select, pattern="^time:"))
        application.add_handler(CallbackQueryHandler(handle_button))
        await application.initialize()
        await application.bot.set_webhook(url=f"{WEBHOOK_URL}{WEBHOOK_PATH}")
        main_loop = asyncio.get_running_loop()
        return True
    except Exception as e:
        logger.error(f"Bot initialization error: {e}")
        return False

if __name__ == '__main__':
    try:
        config = Config()
        config.bind = [f"0.0.0.0:{PORT}"]
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        if not loop.run_until_complete(init_bot()):
            raise RuntimeError("Failed to initialize bot")
        loop.run_until_complete(serve(app, config))
    except KeyboardInterrupt:
        logger.info("Shutdown by user")
    except Exception as e:
        logger.critical(f"Fatal error: {e}")
        raise
    finally:
        try:
            loop.close()
        except Exception:
            pass
