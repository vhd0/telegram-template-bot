import asyncio
import logging
import os
import json
import aiohttp
import urllib.parse
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from dataclasses import dataclass
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes
)

# Logging config
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Constants
MAX_TEXT_LENGTH = 500
RETRY_COUNT = 3
RETRY_DELAY = 1
CACHE_TIMEOUT = 3600  # 1 hour
VERSION = "1.2.1" # Updated version to reflect fix

# Emoji map
EMOJI = {
    'hello': '👋',
    'translate': '🔄',
    'warning': '⚠️',
    'info': 'ℹ️',
    'error': '❌',
    'success': '✅',
    'help': '💡',
    'cache': '💾',
    'time': '⏱️'
}

@dataclass
class CacheEntry:
    text: str
    timestamp: datetime

@dataclass
class TranslationResult:
    original_text: str
    translated_text: Optional[str] = None
    success: bool = False
    error_message: Optional[str] = None
    from_cache: bool = False

class TranslationCache:
    def __init__(self, timeout: int = CACHE_TIMEOUT):
        self.cache: Dict[str, CacheEntry] = {}
        self.timeout = timeout
        
    def get(self, key: str) -> Optional[str]:
        if key in self.cache:
            entry = self.cache[key]
            # Check if the entry is still valid (not expired)
            if datetime.utcnow() - entry.timestamp < timedelta(seconds=self.timeout):
                return entry.text
            # If expired, remove it from cache
            del self.cache[key]
        return None
        
    def set(self, key: str, value: str):
        self.cache[key] = CacheEntry(text=value, timestamp=datetime.utcnow())
        
    def cleanup(self):
        """Removes expired entries from the cache."""
        now = datetime.utcnow()
        expired = [
            k for k, v in self.cache.items()
            if now - v.timestamp >= timedelta(seconds=self.timeout)
        ]
        for k in expired:
            del self.cache[k]
        logger.info(f"Cache cleanup completed. Remaining entries: {len(self.cache.cache)}")


class TranslationService:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.cache = TranslationCache()
        self.last_cleanup = datetime.utcnow()
        self.base_url = "https://api.mymemory.translated.net/get"
        self._initialized = False

    async def ensure_session(self):
        """Ensures an aiohttp client session is active."""
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession()
            self._initialized = True
            logger.info("aiohttp ClientSession initialized.")

    def preprocess_text(self, text: str) -> str:
        """Chuẩn hóa text."""
        return text.strip()

    def postprocess_translation(self, translated: str) -> str:
        """Chuẩn hóa bản dịch."""
        if not translated:
            return translated
            
        # Xử lý HTML entities
        translated = translated.replace("&quot;", '"')
        translated = translated.replace("&#39;", "'")
        translated = translated.replace("&amp;", "&")
        translated = translated.replace("&lt;", "<")
        translated = translated.replace("&gt;", ">")
            
        return translated.strip()

    def maybe_cleanup_cache(self):
        """Triggers cache cleanup if enough time has passed."""
        if datetime.utcnow() - self.last_cleanup > timedelta(hours=1):
            self.cache.cleanup()
            self.last_cleanup = datetime.utcnow()
            logger.info("Scheduled cache cleanup executed.")

    async def get_service_status(self) -> Dict[str, Any]:
        """Get translation service status."""
        return {
            "status": "active" if self._initialized and self.session and not self.session.closed else "inactive",
            "cache_size": len(self.cache.cache),
            "last_cleanup": self.last_cleanup.isoformat()
        }

    async def translate(self, text: str) -> TranslationResult:
        """Dịch văn bản sử dụng MyMemory API."""
        self.maybe_cleanup_cache()
            
        # Check cache
        cached = self.cache.get(text)
        if cached:
            logger.info(f"Translation retrieved from cache for text: {text[:30]}...")
            return TranslationResult(
                original_text=text,
                translated_text=cached,
                success=True,
                from_cache=True
            )
            
        # Prepare translation
        await self.ensure_session()
        processed_text = self.preprocess_text(text)
        result = TranslationResult(original_text=text)
            
        params = {
            "q": processed_text,
            "langpair": "vi|ja",
            "de": "a@b.c"  # Email for better rate limits, as per MyMemory API docs
        }

        # Try translation with retries
        for attempt in range(RETRY_COUNT):
            try:
                async with self.session.get(
                    self.base_url,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10) # 10 seconds timeout for the request
                ) as response:
                    response.raise_for_status() # Raise an exception for bad status codes (4xx or 5xx)
                    data = await response.json()
                            
                    if not data or "responseData" not in data:
                        raise ValueError("Invalid response format from MyMemory API.")
                            
                    translated_text = data["responseData"].get("translatedText")
                    if not translated_text:
                        raise ValueError("Empty translation received from MyMemory API.")
                            
                    # Process the translation
                    result.translated_text = self.postprocess_translation(translated_text)
                    result.success = True
                            
                    # Cache successful translation
                    self.cache.set(text, result.translated_text)
                            
                    logger.info(
                        f"Translation successful (API call): {text[:30]} -> "
                        f"{result.translated_text[:30]}"
                    )
                            
                    return result
            except aiohttp.ClientError as e:
                logger.error(f"HTTP Client Error (attempt {attempt + 1}/{RETRY_COUNT}): {e}")
                result.error_message = f"Lỗi kết nối dịch vụ: {e}"
            except json.JSONDecodeError:
                logger.error(f"Failed to decode JSON response (attempt {attempt + 1}/{RETRY_COUNT}).")
                result.error_message = "Lỗi phản hồi từ dịch vụ dịch."
            except ValueError as e:
                logger.error(f"Translation data error (attempt {attempt + 1}/{RETRY_COUNT}): {e}")
                result.error_message = f"Lỗi dữ liệu dịch: {e}"
            except Exception as e:
                logger.error(f"Unexpected error during translation (attempt {attempt + 1}/{RETRY_COUNT}): {e}")
                result.error_message = "Đã xảy ra lỗi không xác định khi dịch."

            if attempt < RETRY_COUNT - 1:
                await asyncio.sleep(RETRY_DELAY)
            else:
                logger.error(f"All {RETRY_COUNT} translation attempts failed for text: {text[:50]}...")

        result.error_message = result.error_message or "Không thể dịch văn bản. Vui lòng thử lại sau."
        return result


class TelegramBot:
    def __init__(self):
        self.application: Optional[Application] = None
        self.translator = TranslationService()
        self._initialized = False
        self._start_time = datetime.utcnow()
        self._bot_username: Optional[str] = None # Stores the bot's username once initialized

    async def initialize(self, token: str, webhook_url: str) -> bool:
        """Initializes the Telegram bot application."""
        try:
            self.application = (
                ApplicationBuilder()
                .token(token)
                .build()
            )
                
            me = await self.application.bot.get_me() # Get bot info once during initialization
            self._bot_username = me.username
            logger.info(f"Bot info retrieved: @{self._bot_username}")
                
            # Register handlers
            self.application.add_handler(CommandHandler("start", self.start_command))
            self.application.add_handler(CommandHandler("help", self.help_command))
            self.application.add_handler(
                MessageHandler(filters.TEXT & ~filters.COMMAND, self.translate_text)
            )
                
            await self.application.initialize()
            # Set webhook URL for Telegram updates
            await self.application.bot.set_webhook(url=f"{webhook_url}/{token}")
            logger.info(f"Webhook set to {webhook_url}/{token}")
                
            self._initialized = True
            return True
                
        except Exception as e:
            logger.error(f"Bot initialization failed: {str(e)}", exc_info=True)
            return False

    async def get_bot_status(self) -> Dict[str, Any]:
        """Get bot status information without repeatedly calling get_me."""
        status = "inactive"
        username = self._bot_username # Use cached username
        initialized = self._initialized

        if self.application and self.application.bot and initialized:
            status = "active"
            # We don't call get_me() again here to avoid excessive API calls
        else:
            status = "inactive"
        
        return {
            "status": status,
            "username": username,
            "initialized": initialized,
            "start_time": self._start_time.isoformat()
        }

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        logger.info(f"User {user.id} started the bot")
            
        welcome_text = (
            f"{EMOJI['hello']} こんにちは {user.first_name}さん！\n\n"
            f"{EMOJI['info']} Bot dịch văn bản Việt-Nhật\n"
            f"{EMOJI['translate']} Gửi tin nhắn để nhận bản dịch\n"
            f"{EMOJI['help']} Gõ /help để xem hướng dẫn"
        )
        await update.message.reply_text(welcome_text)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.info(f"User {update.effective_user.id} requested help")
            
        help_text = (
            f"{EMOJI['info']} Hướng dẫn sử dụng:\n\n"
            "1. Gửi văn bản tiếng Việt\n"
            "2. Bot sẽ dịch sang tiếng Nhật\n"
            "3. Bản dịch sẽ được gửi riêng để dễ copy\n"
            "4. /start - Bắt đầu sử dụng\n"
            "5. /help - Xem hướng dẫn\n\n"
            f"{EMOJI['help']} Mẹo:\n"
            "• Viết câu đầy đủ và rõ ràng\n"
            f"• Độ dài tối đa {MAX_TEXT_LENGTH} ký tự"
        )
        await update.message.reply_text(help_text)

    async def translate_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        text = update.message.text.strip()
            
        if not text:
            await update.message.reply_text(
                f"{EMOJI['warning']} Vui lòng nhập văn bản để dịch."
            )
            return

        if len(text) > MAX_TEXT_LENGTH:
            await update.message.reply_text(
                f"{EMOJI['warning']} Văn bản quá dài. "
                f"Vui lòng giữ dưới {MAX_TEXT_LENGTH} ký tự."
            )
            return

        logger.info(f"Translating for user {user.id}: '{text[:50]}...'")
            
        try:
            await update.message.chat.send_action("typing") # Show "typing..." status
                
            result = await self.translator.translate(text)
                
            if result.success and result.translated_text:
                # Short confirmation message
                await update.message.reply_text(
                    f"{EMOJI['translate']} Bản dịch:" +
                    (f" {EMOJI['cache']}" if result.from_cache else "") # Indicate if from cache
                )
                    
                # The translation itself in a separate message for easy copying
                await update.message.reply_text(result.translated_text)
                    
                logger.info(f"Translation successful for user {user.id}")
            else:
                await update.message.reply_text(
                    f"{EMOJI['error']} 申し訳ございません。\n"
                    f"{result.error_message or '翻訳エラーが発生しました。'}" # Fallback error message
                )
                logger.error(f"Translation failed for user {user.id}: {result.error_message}")
                
        except Exception as e:
            logger.error(f"Error while translating for user {user.id} (text: '{text[:50]}...'): {e}", exc_info=True)
            await update.message.reply_text(
                f"{EMOJI['error']} エラーが発生しました。\n"
                "Đã xảy ra lỗi. Vui lòng thử lại sau."
            )

# Initialize bot instance globally
bot = TelegramBot()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    webhook_url = os.getenv("WEBHOOK_URL")
        
    if not token or not webhook_url:
        logger.critical("Missing required environment variables: TELEGRAM_BOT_TOKEN or WEBHOOK_URL. Exiting.")
        # This will prevent the application from starting if environment variables are missing
        # For deployment environments, consider more robust secret management.
        raise ValueError("Missing TELEGRAM_BOT_TOKEN or WEBHOOK_URL")
        
    try:
        success = await bot.initialize(token, webhook_url)
        if not success:
            logger.critical("Failed to initialize bot. Application will not start.")
            raise RuntimeError("Failed to initialize bot") # Raise RuntimeError for more specific error
        logger.info("Bot initialized successfully and ready to receive updates.")
    except Exception as e:
        logger.critical(f"Startup failed due to bot initialization error: {e}", exc_info=True)
        raise # Re-raise the exception to prevent the app from starting if init fails
        
    yield # Application is running

    # Shutdown
    logger.info("Application shutdown initiated.")
    try:
        if bot.application:
            await bot.application.shutdown()
            logger.info("Telegram bot application shutdown complete.")
            
        if bot.translator.session:
            await bot.translator.session.close()
            logger.info("Translation service aiohttp session closed.")
    except Exception as e:
        logger.error(f"Error during application shutdown: {e}", exc_info=True)

# Initialize FastAPI app
app = FastAPI(
    title="Telegram Translation Bot",
    description="Bot dịch văn bản Việt-Nhật",
    version=VERSION,
    lifespan=lifespan
)

@app.get("/")
async def root():
    """Root endpoint with basic status."""
    uptime = datetime.utcnow() - bot._start_time
    cache_size = len(bot.translator.cache.cache)
    return {
        "status": "active" if bot._initialized else "initializing",
        "timestamp": datetime.utcnow().isoformat(),
        "version": VERSION,
        "uptime_seconds": uptime.total_seconds(),
        "cache_entries": cache_size,
        "bot_username": bot._bot_username # Include cached username
    }

@app.get("/health")
async def health_check():
    """Enhanced health check endpoint for uptime monitoring."""
    current_time = datetime.utcnow()
    uptime = current_time - bot._start_time
        
    if not bot._initialized:
        logger.warning(f"Health check failed: Bot not initialized. Uptime: {uptime.total_seconds()}s")
        raise HTTPException(
            status_code=503, # Service Unavailable
            detail={
                "status": "error",
                "message": "Bot is not initialized",
                "timestamp": current_time.isoformat(),
                "uptime_seconds": uptime.total_seconds()
            }
        )

    # Get detailed status from bot and translation service
    bot_status = await bot.get_bot_status()
    translation_status = await bot.translator.get_service_status()

    # Determine overall status
    overall_status = "ok"
    if bot_status["status"] != "active":
        overall_status = "degraded"
    if translation_status["status"] != "active":
        overall_status = "degraded"

    response = {
        "status": overall_status,
        "timestamp": current_time.isoformat(),
        "version": VERSION,
        "uptime": {
            "seconds": int(uptime.total_seconds()),
            "formatted": str(uptime).split('.')[0] # HH:MM:SS format
        },
        "services": {
            "bot": bot_status,
            "translation": translation_status
        }
    }
        
    logger.info(f"Health check: {response['status']}")
        
    return response

@app.post("/{token}")
async def telegram_webhook(token: str, request: Request):
    """Handle Telegram webhook requests."""
    if not bot._initialized:
        logger.warning("Received webhook but bot is not initialized. Responding with 503.")
        raise HTTPException(status_code=503, detail="Bot is not initialized")
        
    # Security check: Validate the incoming token
    expected_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not expected_token:
        logger.error("TELEGRAM_BOT_TOKEN environment variable is not set. Cannot validate webhook.")
        raise HTTPException(status_code=500, detail="Server misconfiguration: Bot token not set.")

    if token != expected_token:
        logger.warning(f"Invalid token received in webhook URL: {token[:10]}... (expected {expected_token[:10]}...)")
        raise HTTPException(status_code=403, detail="Invalid token")
        
    try:
        # Parse the incoming update from Telegram
        update = Update.de_json(await request.json(), bot.application.bot)
        # Process the update using the bot's application
        await bot.application.process_update(update)
        return {"status": "ok"}
    except json.JSONDecodeError:
        logger.error("Received webhook with invalid JSON payload.")
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error processing Telegram update: {error_msg}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error processing update: {error_msg}")

if __name__ == "__main__":
    import uvicorn
        
    port = int(os.getenv("PORT", "10000"))
    log_level = os.getenv("LOG_LEVEL", "info")
        
    config = uvicorn.Config(
        "bot:app", # Assumes your file is named 'bot.py'
        host="0.0.0.0",
        port=port,
        workers=1, # Typically 1 worker for Telegram bots to avoid race conditions with updates
        reload=False, # Set to True for development, False for production
        log_level=log_level
    )
        
    server = uvicorn.Server(config)
    server.run()

