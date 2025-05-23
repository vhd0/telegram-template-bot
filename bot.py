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
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler
)
from flask import Flask, request, jsonify
from hypercorn.asyncio import serve
from hypercorn.config import Config
from asgiref.sync import async_to_sync

# --- Settings Management ---
class Settings(BaseSettings):
    """Application settings with validation"""
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

# --- Constants ---
TELEGRAM_CHANNEL = "https://t.me/mikami8186lt"
MESSAGES = {
    "welcome": """三上はじめにへようこそ。以下の選択肢からお選びください。

**ボタンを押した後、処理のためしばらくお待ちください。数秒経っても変化がない場合は、再度ボタンをタップしてください。ありがとうございます。**""",
    "processing": "ボタンを押した後、処理のためしばらくお待ちください。数秒経っても変化がない場合は、再度ボタンをタップしてください。ありがとうございます。",
    "instruction": f"受け取った番号を、到着の10分前までにこちらのチャンネル <a href='{TELEGRAM_CHANNEL}'>Telegramチャネル</a> に送信してください。よろしくお願いいたします！",
    "wait_time": "通常、5分以内に部屋番号をお知らせしますが、担当者が忙しい場合、30分以上お待ちいただくこともございます。恐れ入りますが、しばらくお待ちください。",
    "no_data": "申し訳ありませんが、現在データを利用できません。しばらくしてからもう一度お試しください。",
    "rate_limit": "多くのリクエストを送信しています。しばらくお待ちください。",
    "error": "エラーが発生しました。もう一度お試しください。",
    "restart": "選択肢を始めるか、選択ボードを再起動するには、/start と入力してください。"
}

# --- Initialize Apps ---
settings = Settings()
logger = logging.getLogger(__name__)
flask_app = Flask(__name__)
application = None

# --- Data Models ---
class ExcelData(BaseModel):
    """Data model for Excel rows"""
    Key: str
    Rep1: str
    Rep2: str
    Rep3: str

class BotState:
    """Global state management"""
    def __init__(self):
        self.data: List[dict] = []
        self.string_ids: Dict[str, int] = {}
        self.id_strings: Dict[int, str] = {}
        self.next_id: int = 0
        self.welcomed_users: Set[int] = set()
        self.last_refresh: float = 0
        self._requests: Dict[int, List[float]] = defaultdict(list)
        self.user_messages: Dict[int, Dict[str, int]] = defaultdict(dict)
        self.last_user_activity: Dict[int, float] = {}

    def can_request(self, user_id: int, window: int = 60) -> bool:
        """Check if user can make a request (rate limiting)"""
        now = time.time()
        user_requests = self._requests[user_id]
        
        while user_requests and user_requests[0] < now - window:
            user_requests.pop(0)
        
        if len(user_requests) >= settings.MAX_REQUESTS_PER_MINUTE:
            return False
            
        user_requests.append(now)
        return True

    def get_id(self, text: str) -> int:
        """Get or create ID for text"""
        if not text:
            return -1
            
        if text not in self.string_ids:
            self.string_ids[text] = self.next_id
            self.id_strings[self.next_id] = text
            self.next_id += 1
        return self.string_ids[text]

    def get_string(self, id: int) -> str:
        """Get string from ID"""
        return self.id_strings.get(id, '')

    def update_activity(self, user_id: int) -> None:
        """Update last activity time for user"""
        self.last_user_activity[user_id] = time.time()

    def reset_user(self, user_id: int) -> None:
        """Reset all state for a specific user"""
        self.welcomed_users.discard(user_id)
        self.user_messages.pop(user_id, None)
        self._requests.pop(user_id, None)
        self.update_activity(user_id)

    async def cleanup_user_messages(self, user_id: int, chat_id: int, bot) -> None:
        """Clean up old messages for a user"""
        if user_id in self.user_messages:
            for msg_id in self.user_messages[user_id].values():
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=msg_id)
                except Exception as e:
                    logger.warning(f"Could not delete message {msg_id}: {e}")
            self.user_messages[user_id].clear()

state = BotState()

# --- Data Management ---
@lru_cache(maxsize=1)
def load_excel_data() -> List[dict]:
    """Load and validate Excel data"""
    try:
        file_path = Path(settings.EXCEL_FILE_PATH)
        if not file_path.exists():
            logger.error(f"Excel file not found: {file_path}")
            return []

        df = pd.read_excel(
            file_path,
            engine='openpyxl',
            na_values=[''],
            keep_default_na=False
        )

        required_cols = ["Key", "Rep1", "Rep2", "Rep3"]
        if missing_cols := [col for col in required_cols if col not in df.columns]:
            logger.error(f"Missing columns: {missing_cols}")
            return []

        df = df.fillna('')
        data = df.astype(str).to_dict(orient='records')
        
        valid_data = []
        for row in data:
            try:
                ExcelData(**row)
                valid_data.append(row)
            except Exception as e:
                logger.warning(f"Invalid row data: {e}")
                continue

        logger.info(f"Loaded {len(valid_data)} valid rows")
        return valid_data

    except Exception as e:
        logger.error(f"Excel loading error: {e}")
        return []

def refresh_data() -> None:
    """Refresh cached data if needed"""
    now = time.time()
    if now - state.last_refresh > settings.CACHE_TTL:
        load_excel_data.cache_clear()
        if new_data := load_excel_data():
            state.data = new_data
            state.last_refresh = now
            
            for row in state.data:
                for field in ["Key", "Rep1", "Rep2"]:
                    if row[field]:
                        state.get_id(row[field])
            
            logger.info("Data cache refreshed")
        else:
            logger.warning("No data loaded during refresh")

# --- Message Handlers ---
async def send_initial_buttons(update: Update) -> None:
    """Send welcome message with initial options"""
    try:
        refresh_data()
        
        if not state.data:
            await update.message.reply_text(MESSAGES["no_data"])
            return
        
        initial_keys = {row["Key"] for row in state.data if row["Key"]}
        if not initial_keys:
            await update.message.reply_text(MESSAGES["no_data"])
            return
        
        keyboard = [
            [InlineKeyboardButton(key, callback_data=f"key:{state.get_id(key)}::")]
            for key in sorted(initial_keys)
        ]
        
        # Send new welcome message
        message = await update.message.reply_text(
            MESSAGES["welcome"],
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        
        # Store message ID
        user_id = update.effective_user.id
        state.user_messages[user_id]['welcome'] = message.message_id
        
    except Exception as e:
        logger.error(f"Error sending initial buttons: {e}")
        await update.message.reply_text(MESSAGES["error"])

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command"""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    if not state.can_request(user_id):
        await update.message.reply_text(MESSAGES["rate_limit"])
        return
    
    try:
        # Clean up old messages
        await state.cleanup_user_messages(user_id, chat_id, context.bot)
        
        # Reset user state
        state.reset_user(user_id)
        
        # Send new welcome message
        await send_initial_buttons(update)
        state.welcomed_users.add(user_id)
        state.update_activity(user_id)
        
        logger.info(f"User restarted: {user_id}")
        
    except Exception as e:
        logger.error(f"Error in start handler: {e}")
        await update.message.reply_text(MESSAGES["error"])

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming messages"""
    user_id = update.effective_user.id
    state.update_activity(user_id)
    
    if not state.can_request(user_id):
        await update.message.reply_text(MESSAGES["rate_limit"])
        return
    
    if user_id not in state.welcomed_users:
        await send_initial_buttons(update)
        state.welcomed_users.add(user_id)
        logger.info(f"New user welcomed: {user_id}")
    else:
        await update.message.reply_text(MESSAGES["restart"])

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle button callbacks"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    state.update_activity(user_id)
    
    if not state.can_request(user_id):
        await query.message.reply_text(MESSAGES["rate_limit"])
        return
    
    try:
        async with asyncio.timeout(10):
            refresh_data()
            
            level, *ids = query.data.split(':')
            selected_ids = [int(id_) if id_ else -1 for id_ in ids]
            key_id, rep1_id, rep2_id = selected_ids + [-1] * (3 - len(selected_ids))
            
            key = state.get_string(key_id)
            rep1 = state.get_string(rep1_id) if rep1_id != -1 else ''
            rep2 = state.get_string(rep2_id) if rep2_id != -1 else ''
            
            if level == "key":
                next_rep1 = {
                    row["Rep1"] for row in state.data
                    if row["Key"] == key and row["Rep1"]
                }
                
                if next_rep1:
                    keyboard = [
                        [InlineKeyboardButton(
                            r1,
                            callback_data=f"rep1:{key_id}:{state.get_id(r1)}:"
                        )]
                        for r1 in sorted(next_rep1)
                    ]
                    
                    await query.edit_message_text(
                        f"選択されました: {key}\n{MESSAGES['processing']}\n\n次に進んでください:",
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                else:
                    await query.edit_message_text(
                        f"選択されました: {key}\n{MESSAGES['no_data']}"
                    )
            
            elif level == "rep1":
                next_rep2 = {
                    row["Rep2"] for row in state.data
                    if row["Key"] == key and row["Rep1"] == rep1 and row["Rep2"]
                }
                
                if next_rep2:
                    keyboard = [
                        [InlineKeyboardButton(
                            r2,
                            callback_data=f"rep2:{key_id}:{rep1_id}:{state.get_id(r2)}"
                        )]
                        for r2 in sorted(next_rep2)
                    ]
                    
                    await query.edit_message_text(
                        f"選択されました: {rep1}\n{MESSAGES['processing']}\n\n次に進んでください:",
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                else:
                    await query.edit_message_text(
                        f"選択されました: {rep1}\n{MESSAGES['no_data']}"
                    )
            
            elif level == "rep2":
                rep3 = next(
                    (row["Rep3"] for row in state.data
                     if row["Key"] == key and
                     row["Rep1"] == rep1 and
                     row["Rep2"] == rep2),
                    MESSAGES["no_data"]
                )
                
                await query.edit_message_text(f"あなたの番号: {rep3}")
                
                # Store final message
                final_msg = await query.message.reply_text(
                    f"{MESSAGES['instruction']}\n\n{MESSAGES['wait_time']}",
                    parse_mode='HTML'
                )
                state.user_messages[user_id]['final'] = final_msg.message_id
            
            else:
                await query.edit_message_text(MESSAGES["error"])
                
    except asyncio.TimeoutError:
        logger.error(f"Timeout for user {user_id}")
        await query.message.reply_text(MESSAGES["error"])
    except Exception as e:
        logger.error(f"Button handler error: {e}")
        await query.message.reply_text(MESSAGES["error"])

async def cleanup_old_messages(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cleanup old messages periodically"""
    try:
        current_time = time.time()
        for user_id in list(state.last_user_activity.keys()):
            if current_time - state.last_user_activity[user_id] > 3600:  # 1 hour
                try:
                    chat_id = user_id  # In private chats, chat_id equals user_id
                    await state.cleanup_user_messages(user_id, chat_id, context.bot)
                    state.reset_user(user_id)
                except Exception as e:
                    logger.warning(f"Could not cleanup for user {user_id}: {e}")
    except Exception as e:
        logger.error(f"Error in cleanup task: {e}")

# --- Flask Routes ---
@flask_app.route(settings.WEBHOOK_PATH, methods=["POST"])
def webhook_handler():
    """Handle Telegram webhook updates"""
    if not application:
        return "Bot not ready", 503

    try:
        if not (data := request.get_json(force=True)):
            return "Empty request", 400

        async_to_sync(application.process_update)(
            Update.de_json(data, application.bot)
        )
        return "ok", 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "ok", 200

@flask_app.route("/health")
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "1.0.0",
        "data_status": "loaded" if state.data else "empty",
        "last_refresh": datetime.fromtimestamp(state.last_refresh).isoformat() if state.last_refresh else None,
        "active_users": len(state.welcomed_users)
    })

# --- Error Handler ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler"""
    logger.error("Update error:", exc_info=context.error)

# --- Application Startup ---
async def init_application():
    """Initialize application"""
    global application

    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.DEBUG if settings.DEBUG else logging.INFO,
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    try:
        application = ApplicationBuilder().token(settings.BOT_TOKEN).build()

        application.add_handler(CommandHandler("start", handle_start))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        application.add_handler(CallbackQueryHandler(handle_button))
        application.add_error_handler(error_handler)

        # Add cleanup job
        job_queue = application.job_queue
        job_queue.run_repeating(cleanup_old_messages, interval=3600)

        await application.initialize()
        webhook_url = f"{settings.WEBHOOK_URL}{settings.WEBHOOK_PATH}"
        await application.bot.set_webhook(url=webhook_url)
        logger.info(f"Webhook set: {webhook_url}")

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
