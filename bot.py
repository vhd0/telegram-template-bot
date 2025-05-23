import logging
import os
import asyncio
import pandas as pd
import time
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Set, Optional
from functools import lru_cache
from datetime import datetime, timezone
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, 
    ContextTypes, filters, CallbackQueryHandler
)
from flask import Flask, request, jsonify
from hypercorn.asyncio import serve
from hypercorn.config import Config
from asgiref.sync import async_to_sync

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

MESSAGES = {
    "welcome": """三上はじめにへようこそ。以下の選択肢からお選びください。\n\n**ボタンを押した後、処理のためしばらくお待ちください。数秒経っても変化がない場合は、再度ボタンをタップしてください。ありがとうございます。**""",
    "processing": "⏳ 処理中です...",
    "next_step": "次に進んでください:",
    "selected": "選択されました: {}",
    "instruction": "受け取った番号を、到着の10分前までにこちらのチャンネル <a href='https://t.me/mikami8186lt'>Telegramチャネル</a> に送信してください。よろしくお願いいたします！",
    "wait_time": "通常、5分以内に部屋番号をお知らせしますが、担当者が忙しい場合、30分以上お待ちいただくこともございます。恐れ入りますが、しばらくお待ちください。",
    "no_data": "申し訳ありませんが、現在データを利用できません。",
    "rate_limit": "多くのリクエストを送信しています。しばらくお待ちください。",
    "error": "エラーが発生しました。もう一度お試しください。",
    "number": "{}"
}

class State:
    def __init__(self):
        self.data: List[dict] = []
        self.string_ids: Dict[str, int] = {}
        self.id_strings: Dict[int, str] = {}
        self.next_id: int = 0
        self.welcomed_users: Set[int] = set()
        self.last_refresh: float = 0
        self._requests: Dict[int, List[float]] = defaultdict(list)
        self.processing: Dict[int, bool] = {}

    def can_request(self, user_id: int) -> bool:
        now = time.time()
        requests = self._requests[user_id] = [r for r in self._requests[user_id] if now - r < 60]
        if len(requests) >= settings.MAX_REQUESTS_PER_MINUTE:
            return False
        requests.append(now)
        return True

    def get_id(self, text: str) -> int:
        if not text:
            return -1
        if text not in self.string_ids:
            self.string_ids[text] = self.next_id
            self.id_strings[self.next_id] = text
            self.next_id += 1
        return self.string_ids[text]

    def get_string(self, id: int) -> str:
        return self.id_strings.get(id, '')

settings = Settings()
logger = logging.getLogger(__name__)
flask_app = Flask(__name__)
application = None
state = State()

@lru_cache(maxsize=1)
def load_excel_data() -> List[dict]:
    try:
        df = pd.read_excel(settings.EXCEL_FILE_PATH, engine='openpyxl', na_values=[''])
        df = df.fillna('')
        return df.astype(str).to_dict(orient='records')
    except Exception as e:
        logger.error(f"Excel loading error: {e}")
        return []

def refresh_data() -> None:
    now = time.time()
    if now - state.last_refresh > settings.CACHE_TTL:
        load_excel_data.cache_clear()
        if data := load_excel_data():
            state.data = data
            state.last_refresh = now
            for row in data:
                for field in ["Key", "Rep1", "Rep2"]:
                    if row[field]:
                        state.get_id(row[field])

async def safe_send_message(func, *args, **kwargs):
    """Safely execute telegram bot API calls"""
    max_retries = 3
    retry_delay = 1

    for attempt in range(max_retries):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            if attempt == max_retries - 1:
                logger.error(f"Failed after {max_retries} attempts: {e}")
                raise
            logger.warning(f"Attempt {attempt + 1} failed: {e}")
            await asyncio.sleep(retry_delay)

async def send_initial_buttons(update: Update) -> None:
    refresh_data()
    if not state.data:
        await safe_send_message(update.message.reply_text, MESSAGES["no_data"])
        return

    keyboard = [
        [InlineKeyboardButton(key, callback_data=f"key:{state.get_id(key)}::")]
        for key in sorted({row["Key"] for row in state.data if row["Key"]})
    ]
    
    await safe_send_message(
        update.message.reply_text,
        MESSAGES["welcome"],
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    
    if not state.can_request(user_id):
        await safe_send_message(update.message.reply_text, MESSAGES["rate_limit"])
        return
    
    state.welcomed_users.discard(user_id)
    await send_initial_buttons(update)
    state.welcomed_users.add(user_id)

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id
    
    if not state.can_request(user_id) or state.processing.get(user_id):
        await safe_send_message(query.answer, MESSAGES["processing"])
        return

    try:
        state.processing[user_id] = True
        await safe_send_message(query.answer)
        refresh_data()

        level, *ids = query.data.split(':')
        selected_ids = [int(id_) if id_ else -1 for id_ in ids]
        key_id, rep1_id, rep2_id = selected_ids + [-1] * (3 - len(selected_ids))

        key = state.get_string(key_id)
        rep1 = state.get_string(rep1_id) if rep1_id != -1 else ''
        rep2 = state.get_string(rep2_id) if rep2_id != -1 else ''

        if level == "key":
            next_rep1 = {row["Rep1"] for row in state.data if row["Key"] == key and row["Rep1"]}
            if next_rep1:
                keyboard = [[InlineKeyboardButton(r1, callback_data=f"rep1:{key_id}:{state.get_id(r1)}:")] 
                          for r1 in sorted(next_rep1)]
                await safe_send_message(
                    query.edit_message_text,
                    f"{MESSAGES['selected'].format(key)}\n{MESSAGES['next_step']}",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
        
        elif level == "rep1":
            next_rep2 = {row["Rep2"] for row in state.data 
                        if row["Key"] == key and row["Rep1"] == rep1 and row["Rep2"]}
            if next_rep2:
                keyboard = [[InlineKeyboardButton(r2, callback_data=f"rep2:{key_id}:{rep1_id}:{state.get_id(r2)}")] 
                          for r2 in sorted(next_rep2)]
                await safe_send_message(
                    query.edit_message_text,
                    f"{MESSAGES['selected'].format(rep1)}\n{MESSAGES['next_step']}",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
        
        elif level == "rep2":
            rep3 = next((row["Rep3"] for row in state.data 
                        if row["Key"] == key and row["Rep1"] == rep1 and row["Rep2"] == rep2),
                       MESSAGES["no_data"])
            await safe_send_message(query.edit_message_text, MESSAGES["number"].format(rep3))
            await safe_send_message(
                query.message.reply_text,
                f"{MESSAGES['instruction']}\n\n{MESSAGES['wait_time']}",
                parse_mode='HTML'
            )

    except Exception as e:
        logger.error(f"Button handler error: {e}")
        await safe_send_message(query.message.reply_text, MESSAGES["error"])
    finally:
        state.processing[user_id] = False

async def process_update(update_dict: dict) -> None:
    """Process update with proper event loop handling"""
    try:
        update = Update.de_json(update_dict, application.bot)
        await application.process_update(update)
    except Exception as e:
        logger.error(f"Error processing update: {e}")

def run_async(coro):
    """Run coroutine in the event loop"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

@flask_app.route(settings.WEBHOOK_PATH, methods=["POST"])
def webhook_handler():
    """Handle Telegram webhook updates"""
    if not application:
        return "Bot not ready", 503

    try:
        if data := request.get_json(force=True):
            run_async(process_update(data))
        return "ok", 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "ok", 200

@flask_app.route("/health")
def health_check():
    return jsonify({
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "1.0.0",
        "active_users": len(state.welcomed_users)
    })

async def init_application():
    """Initialize application"""
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
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, 
                                            lambda u, c: u.message.reply_text(MESSAGES["welcome"])))
        application.add_handler(CallbackQueryHandler(handle_button))

        await application.initialize()
        await application.bot.set_webhook(url=f"{settings.WEBHOOK_URL}{settings.WEBHOOK_PATH}")
        refresh_data()
        return True

    except Exception as e:
        logger.critical(f"Initialization error: {e}")
        return False

async def run_application():
    """Run the application"""
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
