import logging
import os
import asyncio
import pandas as pd
import time
from collections import defaultdict
from typing import Dict, List, Set
from functools import lru_cache
from datetime import datetime, timezone
from pydantic_settings import BaseSettings
from pydantic import Field
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler
)
from flask import Flask, request, jsonify
from hypercorn.asyncio import serve
from hypercorn.config import Config

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
ADMIN_ID = 8149389037

MESSAGES = {
    "welcome": "三上はじめにへようこそ。下記の選択肢からご希望の項目をお選びください。\n\n※ボタンを押した後、処理に数秒かかる場合がございます。しばらくお待ちいただくか、反応がない場合は再度ボタンを押してください。ご協力ありがとうございます。",
    "processing": "⏳ 只今処理中です。しばらくお待ちください。",
    "next_step": "次の項目をお選びください。",
    "selected": "選択された項目：{}",
    "instruction": "受け取った番号を、到着の10分前までに下記のチャンネル <a href='https://t.me/mikami8186lt'>Telegramチャネル</a> へご送信ください。何卒よろしくお願いいたします。",
    "wait_time": "通常、5分以内にお部屋番号をご案内いたしますが、担当者が対応中の場合30分以上お待ちいただくことがございます。誠に恐れ入りますが、今しばらくお待ちくださいませ。",
    "no_data": "申し訳ございませんが、現在ご利用いただけるデータがありません。",
    "rate_limit": "リクエストが多すぎます。しばらく時間をおいてから再度お試しください。",
    "error": "エラーが発生しました。お手数ですが、もう一度お試しください。",
    "number": "お客様の番号：{}"
}

class State:
    def __init__(self):
        self.data: List[dict] = []
        self.string_ids: Dict[str, int] = {}
        self.id_strings: Dict[int, str] = {}
        self.next_id = 0
        self.welcomed_users: Set[int] = set()
        self.last_refresh = 0
        self._requests = defaultdict(list)
        self.processing = {}

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

@lru_cache(maxsize=1)
def load_excel_data() -> List[dict]:
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
        if data := load_excel_data():
            state.data = data
            state.last_refresh = now
            for row in data:
                for field in ["Key", "Rep1", "Rep2"]:
                    if row[field]: state.get_id(row[field])

def get_display_name(user):
    if getattr(user, 'full_name', None) and user.full_name.strip():
        return user.full_name.strip()
    if getattr(user, 'username', None):
        return f"@{user.username}"
    return str(user.id)

def get_tag(user):
    return f"@{user.username}" if user.username else f"<a href='tg://user?id={user.id}'>user</a>"

async def safe_send(func, *args, **kwargs):
    try:
        return await func(*args, **kwargs)
    except Exception as e:
        logger.warning(f"Send error: {e}")

async def send_initial_buttons(update: Update):
    refresh_data()
    if not state.data:
        await safe_send(update.message.reply_text, MESSAGES["no_data"])
        return
    keys = sorted({row["Key"] for row in state.data if row["Key"]})
    keyboard = [[InlineKeyboardButton(k, callback_data=f"key:{state.get_id(k)}::")] for k in keys]
    await safe_send(
        update.message.reply_text,
        MESSAGES["welcome"],
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not state.can_request(user_id):
        await safe_send(update.message.reply_text, MESSAGES["rate_limit"])
        return
    state.welcomed_users.discard(user_id)
    await send_initial_buttons(update)
    state.welcomed_users.add(user_id)

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    user_id = user.id
    display_name = get_display_name(user)
    tag = get_tag(user)
    if not state.can_request(user_id) or state.processing.get(user_id):
        await safe_send(query.answer, MESSAGES["processing"])
        return
    try:
        state.processing[user_id] = True
        await safe_send(query.answer)
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
                await safe_send(query.edit_message_text, f"{MESSAGES['selected'].format(key)}\n{MESSAGES['next_step']}", reply_markup=InlineKeyboardMarkup(keyboard))
        elif level == "rep1":
            rep2s = sorted({row["Rep2"] for row in state.data if row["Key"] == key and row["Rep1"] == rep1 and row["Rep2"]})
            if rep2s:
                keyboard = [[InlineKeyboardButton(r2, callback_data=f"rep2:{key_id}:{rep1_id}:{state.get_id(r2)}")] for r2 in rep2s]
                await safe_send(query.edit_message_text, f"{MESSAGES['selected'].format(rep1)}\n{MESSAGES['next_step']}", reply_markup=InlineKeyboardMarkup(keyboard))
        elif level == "rep2":
            rep3 = next((row["Rep3"] for row in state.data if row["Key"] == key and row["Rep1"] == rep1 and row["Rep2"] == rep2), MESSAGES["no_data"])
            await safe_send(query.edit_message_text, MESSAGES["number"].format(rep3))

            # メッセージ例: 山田太郎 (@yamada) - 12345
            msg = f"{display_name}（{tag}） - {rep3}"
            await safe_send(context.bot.send_message, chat_id=CHANNEL_ID, text=msg, parse_mode='HTML')
            
            # 30分後に自動で退出（管理者以外）
            if user_id != ADMIN_ID:
                async def delayed_kick():
                    await asyncio.sleep(30 * 60)
                    try:
                        await context.bot.ban_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
                        await context.bot.unban_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
                    except Exception as e:
                        logger.error(f"Kick user error: {e}")
                asyncio.create_task(delayed_kick())
            await safe_send(query.message.reply_text, f"{MESSAGES['instruction']}\n\n{MESSAGES['wait_time']}", parse_mode='HTML')
    except Exception as e:
        logger.error(f"Button handler error: {e}")
        await safe_send(query.message.reply_text, MESSAGES["error"])
    finally:
        state.processing[user_id] = False

def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

@flask_app.route(settings.WEBHOOK_PATH, methods=["POST"])
def webhook_handler():
    if not application: return "Bot not ready", 503
    try:
        data = request.get_json(force=True)
        if data:
            run_async(process_update(data))
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
        "version": "1.0.0",
        "active_users": len(state.welcomed_users)
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
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: u.message.reply_text(MESSAGES["welcome"])))
        application.add_handler(CallbackQueryHandler(handle_button))
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
