import logging
import os
import asyncio
import pandas as pd
import time
from collections import defaultdict
from typing import Dict, List, Set, Optional
from functools import lru_cache
from datetime import datetime, timezone
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings  # Changed this import
from tenacity import retry, stop_after_attempt, wait_exponential, RetryError

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

# ... rest of the code remains the same ...

# --- Custom Exceptions ---
class BotError(Exception):
    """Base exception for bot errors"""
    pass

class ConfigError(BotError):
    """Configuration related errors"""
    pass

class DataError(BotError):
    """Data processing related errors"""
    pass

# --- Configuration Management ---
class Settings(BaseSettings):
    """Application settings with validation"""
    BOT_TOKEN: str = Field(..., description="Telegram Bot Token")
    WEBHOOK_URL: str = Field(..., description="Webhook URL for Telegram updates")
    PORT: int = Field(default=int(os.getenv('PORT', 8443)), description="Port number")
    EXCEL_FILE_PATH: str = Field(default="rep.xlsx", description="Path to Excel data file")
    MAX_REQUESTS_PER_MINUTE: int = Field(default=30, description="Rate limit per user")
    CACHE_TTL: int = Field(default=300, description="Cache TTL in seconds")
    WEBHOOK_PATH: str = Field(default="/webhook_telegram", description="Webhook endpoint path")
    DEBUG: bool = Field(default=False, description="Debug mode flag")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True

# --- Constants ---
TELEGRAM_CHANNEL_LINK = "https://t.me/mikami8186lt"
MESSAGE_TEMPLATES = {
    "welcome": """三上はじめにへようこそ。以下の選択肢からお選びください。

**ボタンを押した後、処理のためしばらくお待ちください。数秒経っても変化がない場合は、再度ボタンをタップしてください。ありがとうございます。**""",
    "processing": "ボタンを押した後、処理のためしばらくお待ちください。数秒経っても変化がない場合は、再度ボタンをタップしてください。ありがとうございます。",
    "instruction": f"受け取った番号を、到着の10分前までにこちらのチャンネル <a href='{TELEGRAM_CHANNEL_LINK}'>Telegramチャネル</a> に送信してください。よろしくお願いいたします！",
    "wait_time": "通常、5分以内に部屋番号をお知らせしますが、担当者が忙しい場合、30分以上お待ちいただくこともございます。恐れ入りますが、しばらくお待ちください。",
    "unrecognized": "何を言っているのか分かりません。選択肢を始めるか、選択ボードを再起動するには、/start と入力してください。",
    "timeout": "処理がタイムアウトしました。もう一度お試しください。",
    "rate_limit": "多くのリクエストを送信しています。しばらくお待ちください。",
    "error": "エラーが発生しました。もう一度お試しください。",
    "no_info": "情報が見つかりません。",
    "invalid_operation": "不明な操作です。"
}

# --- Global Variables ---
settings = Settings()
logger = logging.getLogger(__name__)
flask_app = Flask(__name__)
application = None

class GlobalState:
    """Global state management"""
    def __init__(self):
        self.data_table: List[dict] = []
        self.string_to_id: Dict[str, int] = {}
        self.id_to_string: Dict[int, str] = {}
        self.next_id: int = 0
        self.welcomed_users: Set[int] = set()
        self.last_data_refresh: float = 0
        self.rate_limiter = self.RateLimiter()

    class RateLimiter:
        """Rate limiting implementation"""
        def __init__(self):
            self.requests: Dict[int, List[float]] = defaultdict(list)
            self.window = 60  # 1 minute window

        def is_allowed(self, user_id: int) -> bool:
            now = time.time()
            user_requests = self.requests[user_id]
            
            # Clean old requests
            while user_requests and user_requests[0] < now - self.window:
                user_requests.pop(0)
            
            if len(user_requests) >= settings.MAX_REQUESTS_PER_MINUTE:
                return False
            
            user_requests.append(now)
            return True

state = GlobalState()

# --- Data Models ---
class ExcelRow(BaseModel):
    """Data model for Excel rows"""
    Key: str
    Rep1: str
    Rep2: str
    Rep3: str

# --- Data Management ---
@lru_cache(maxsize=1)
def load_excel_data() -> List[dict]:
    """Load and validate Excel data"""
    try:
        df = pd.read_excel(settings.EXCEL_FILE_PATH)
        required_columns = ["Key", "Rep1", "Rep2", "Rep3"]
        
        if not all(col in df.columns for col in required_columns):
            raise DataError(f"Missing required columns: {', '.join(required_columns)}")

        df = df.fillna('')
        data = df.astype(str).to_dict(orient='records')
        
        # Validate data
        for row in data:
            ExcelRow(**row)
        
        return data

    except Exception as e:
        logger.error(f"Excel data loading error: {e}")
        raise DataError(f"Failed to load Excel data: {str(e)}")

def get_or_create_id(text: str) -> int:
    """Get or create ID for text value"""
    if not text:
        return -1
        
    if text not in state.string_to_id:
        state.string_to_id[text] = state.next_id
        state.id_to_string[state.next_id] = text
        state.next_id += 1
    return state.string_to_id[text]

def refresh_data_if_needed() -> None:
    """Refresh cached data if TTL expired"""
    current_time = time.time()
    if current_time - state.last_data_refresh > settings.CACHE_TTL:
        load_excel_data.cache_clear()
        state.data_table = load_excel_data()
        state.last_data_refresh = current_time
        
        # Rebuild ID mappings
        for row in state.data_table:
            for field in ["Key", "Rep1", "Rep2"]:
                if row[field]:
                    get_or_create_id(row[field])

# --- Message Handlers ---
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    reraise=True
)
async def send_message_with_retry(update: Update, text: str, reply_markup=None, parse_mode=None) -> None:
    """Send message with retry mechanism"""
    try:
        await update.message.reply_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode
        )
    except Exception as e:
        logger.error(f"Message sending error: {e}")
        raise

async def send_initial_buttons(update: Update) -> None:
    """Send welcome message with initial buttons"""
    refresh_data_if_needed()
    
    initial_keys = {row["Key"] for row in state.data_table if row["Key"]}
    keyboard = [
        [InlineKeyboardButton(key, callback_data=f"key:{get_or_create_id(key)}::")]
        for key in sorted(initial_keys)
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    user_id = update.effective_user.id
    
    logger.info(f"Sending welcome message to user {user_id}")
    await send_message_with_retry(
        update,
        MESSAGE_TEMPLATES["welcome"],
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming messages"""
    user_id = update.effective_user.id
    
    if not state.rate_limiter.is_allowed(user_id):
        await update.message.reply_text(MESSAGE_TEMPLATES["rate_limit"])
        return
    
    if user_id not in state.welcomed_users:
        await send_initial_buttons(update)
        state.welcomed_users.add(user_id)
        logger.info(f"New user welcomed: {user_id}")
        return
    
    if update.message and update.message.text:
        logger.info(f"Unexpected message from user {user_id}: {update.message.text}")
        await update.message.reply_text(MESSAGE_TEMPLATES["unrecognized"])

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command"""
    user_id = update.effective_user.id
    
    if not state.rate_limiter.is_allowed(user_id):
        await update.message.reply_text(MESSAGE_TEMPLATES["rate_limit"])
        return
    
    if user_id in state.welcomed_users:
        state.welcomed_users.remove(user_id)
        logger.info(f"User {user_id} restarted session")
    
    await send_initial_buttons(update)
    state.welcomed_users.add(user_id)

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle button callbacks"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    if not state.rate_limiter.is_allowed(user_id):
        await query.message.reply_text(MESSAGE_TEMPLATES["rate_limit"])
        return
    
    try:
        async with asyncio.timeout(10):
            refresh_data_if_needed()
            
            # Parse callback data
            level, *ids = query.data.split(':')
            selected_ids = [int(id_) if id_ else -1 for id_ in ids]
            key_id, rep1_id, rep2_id = selected_ids + [-1] * (3 - len(selected_ids))
            
            # Get display values
            key = state.id_to_string.get(key_id, '')
            rep1 = state.id_to_string.get(rep1_id, '') if rep1_id != -1 else ''
            rep2 = state.id_to_string.get(rep2_id, '') if rep2_id != -1 else ''
            
            logger.info(
                f"Button press: {level}, "
                f"Key={key}({key_id}), "
                f"Rep1={rep1}({rep1_id}), "
                f"Rep2={rep2}({rep2_id})"
            )
            
            if level == "key":
                next_rep1 = {
                    row["Rep1"] for row in state.data_table
                    if row["Key"] == key and row["Rep1"]
                }
                
                if next_rep1:
                    keyboard = [
                        [InlineKeyboardButton(
                            r1,
                            callback_data=f"rep1:{key_id}:{get_or_create_id(r1)}:"
                        )]
                        for r1 in sorted(next_rep1)
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    message = f"選択されました: {key}\n{MESSAGE_TEMPLATES['processing']}\n\n次に進んでください:"
                    await query.edit_message_text(
                        text=message,
                        reply_markup=reply_markup
                    )
                else:
                    await query.edit_message_text(
                        f"選択されました: {key}\n{MESSAGE_TEMPLATES['no_info']}"
                    )
            
            elif level == "rep1":
                next_rep2 = {
                    row["Rep2"] for row in state.data_table
                    if row["Key"] == key and row["Rep1"] == rep1 and row["Rep2"]
                }
                
                if next_rep2:
                    keyboard = [
                        [InlineKeyboardButton(
                            r2,
                            callback_data=f"rep2:{key_id}:{rep1_id}:{get_or_create_id(r2)}"
                        )]
                        for r2 in sorted(next_rep2)
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    message = f"選択されました: {rep1}\n{MESSAGE_TEMPLATES['processing']}\n\n次に進んでください:"
                    await query.edit_message_text(
                        text=message,
                        reply_markup=reply_markup
                    )
                else:
                    await query.edit_message_text(
                        f"選択されました: {rep1}\n{MESSAGE_TEMPLATES['no_info']}"
                    )
            
            elif level == "rep2":
                rep3 = next(
                    (row["Rep3"] for row in state.data_table
                     if row["Key"] == key and
                     row["Rep1"] == rep1 and
                     row["Rep2"] == rep2),
                    MESSAGE_TEMPLATES["no_info"]
                )
                
                await query.edit_message_text(f"あなたの番号: {rep3}")
                
                instructions = f"{MESSAGE_TEMPLATES['instruction']}\n\n{MESSAGE_TEMPLATES['wait_time']}"
                await query.message.reply_text(
                    text=instructions,
                    parse_mode='HTML'
                )
            
            else:
                await query.edit_message_text(MESSAGE_TEMPLATES["invalid_operation"])
                
    except asyncio.TimeoutError:
        logger.error("Operation timeout")
        await query.message.reply_text(MESSAGE_TEMPLATES["timeout"])
    except Exception as e:
        logger.error(f"Button handler error: {e}")
        await query.message.reply_text(MESSAGE_TEMPLATES["error"])

# --- Flask Routes ---
@flask_app.route(settings.WEBHOOK_PATH, methods=["POST"])
async def webhook_handler():
    """Handle Telegram webhook updates"""
    if application is None:
        logger.error("Application not initialized")
        return "Bot not ready", 500

    try:
        json_data = request.get_json(force=True)
        if not json_data:
            logger.warning("Empty webhook payload")
            return "Bad Request", 400

        update = Update.de_json(json_data, application.bot)
        await application.process_update(update)
        return "ok", 200
    except Exception as e:
        logger.error(f"Webhook processing error: {e}")
        return "ok", 200

@flask_app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "1.0.0"
    })

# --- Error Handler ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler"""
    logger.error("Update processing error:", exc_info=context.error)

# --- Application Startup ---
async def initialize_application():
    """Initialize and configure the application"""
    global application

    try:
        if not settings.BOT_TOKEN:
            raise ConfigError("Missing BOT_TOKEN")
        if not settings.WEBHOOK_URL:
            raise ConfigError("Missing WEBHOOK_URL")

        # Configure logging
        logging.basicConfig(
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            level=logging.DEBUG if settings.DEBUG else logging.INFO,
            datefmt="%Y-%m-%d %H:%M:%S"
        )

        # Initialize bot
        application = ApplicationBuilder().token(settings.BOT_TOKEN).build()

        # Add handlers
        application.add_handler(CommandHandler("start", handle_start))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        application.add_handler(CallbackQueryHandler(handle_button))
        application.add_error_handler(error_handler)

        # Initialize bot and set webhook
        await application.initialize()
        webhook_url = f"{settings.WEBHOOK_URL}{settings.WEBHOOK_PATH}"
        logger.info(f"Setting webhook: {webhook_url}")
        
        await application.bot.set_webhook(url=webhook_url)
        logger.info("Webhook set successfully")

        # Load initial data
        refresh_data_if_needed()
        logger.info("Initial data loaded")

        return True

    except Exception as e:
        logger.critical(f"Initialization error: {e}")
        raise

async def run_application():
    """Run the application"""
    try:
        if await initialize_application():
            config = Config()
            config.bind = [f"0.0.0.0:{settings.PORT}"]
            
            logger.info(f"Starting server on port {settings.PORT}")
            await serve(flask_app, config)
    except Exception as e:
        logger.critical(f"Application startup error: {e}")
        raise

if __name__ == '__main__':
    try:
        asyncio.run(run_application())
    except KeyboardInterrupt:
        logger.info("Application stopped by user")
    except Exception as e:
        logger.critical(f"Fatal error: {e}")
        raise
