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

# Logging configuration
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO # Default logging level remains INFO
)
logger = logging.getLogger(__name__)

# Constants
MAX_TEXT_LENGTH = 500
RETRY_COUNT = 3
RETRY_DELAY = 1
CACHE_TIMEOUT = 3600  # 1 hour
VERSION = "1.2.5" # Updated version to reflect more robust Uvicorn log filtering

# Emoji map for messages
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
        # Session is now managed externally and set via set_session
        self.session: Optional[aiohttp.ClientSession] = None 
        self.cache = TranslationCache()
        self.last_cleanup = datetime.utcnow()
        self.base_url = "https://api.mymemory.translated.net/get"
        # _initialized now depends on the session being provided/set
        self._initialized = False

    def set_session(self, session: aiohttp.ClientSession):
        """Sets the aiohttp client session for the translation service."""
        self.session = session
        self._initialized = True
        logger.info("aiohttp ClientSession set for TranslationService.")

    def preprocess_text(self, text: str) -> str:
        """Standardizes the text for translation."""
        return text.strip()

    def postprocess_translation(self, translated: str) -> str:
        """Standardizes the translated text."""
        if not translated:
            return translated
            
        # Handle common HTML entities
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
        """Gets the status of the translation service."""
        return {
            "status": "active" if self._initialized and self.session and not self.session.closed else "inactive",
            "cache_size": len(self.cache.cache),
            "last_cleanup": self.last_cleanup.isoformat()
        }

    async def translate(self, text: str) -> TranslationResult:
        """Translates text using the MyMemory API."""
        self.maybe_cleanup_cache()
            
        # Check cache first
        cached = self.cache.get(text)
        if cached:
            logger.info(f"Translation retrieved from cache for text: {text[:30]}...")
            return TranslationResult(
                original_text=text,
                translated_text=cached,
                success=True,
                from_cache=True
            )
            
        # Ensure session is available before making API call
        if not self.session or self.session.closed:
            # This should ideally not happen if lifespan manages the session correctly
            logger.error("TranslationService session is not active. Cannot translate.")
            return TranslationResult(
                original_text=text,
                success=False,
                error_message="Dịch vụ dịch chưa sẵn sàng."
            )

        processed_text = self.preprocess_text(text)
        result = TranslationResult(original_text=text)
            
        params = {
            "q": processed_text,
            "langpair": "vi|ja", # Translate from Vietnamese to Japanese
            "de": "a@b.c"  # Email for better rate limits, as per MyMemory API docs
        }

        # Attempt translation with retries
        for attempt in range(RETRY_COUNT):
            try:
                async with self.session.get(
                    self.base_url,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10) # 10 seconds timeout for the request
                ) as response:
                    response.raise_for_status() # Raise an exception for bad HTTP status codes (4xx or 5xx)
                    data = await response.json()
                            
                    if not data or "responseData" not in data:
                        raise ValueError("Invalid response format from MyMemory API.")
                            
                    translated_text = data["responseData"].get("translatedText")
                    if not translated_text:
                        raise ValueError("Empty translation received from MyMemory API.")
                            
                    # Process and store the translation
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
                logger.error(f"HTTP Client Error (attempt {attempt + 1}/{RETRY_COUNT}): {e}", exc_info=True)
                result.error_message = f"Lỗi kết nối dịch vụ: {e}"
            except json.JSONDecodeError:
                logger.error(f"Failed to decode JSON response (attempt {attempt + 1}/{RETRY_COUNT}).", exc_info=True)
                result.error_message = "Lỗi phản hồi từ dịch vụ dịch."
            except ValueError as e:
                logger.error(f"Translation data error (attempt {attempt + 1}/{RETRY_COUNT}): {e}", exc_info=True)
                result.error_message = f"Lỗi dữ liệu dịch: {e}"
            except Exception as e:
                logger.error(f"Unexpected error during translation (attempt {attempt + 1}/{RETRY_COUNT}): {e}", exc_info=True)
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
        self.translator = TranslationService() # TranslationService instance
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
                
            # Register handlers for commands and text messages
            self.application.add_handler(CommandHandler("start", self.start_command))
            self.application.add_handler(CommandHandler("help", self.help_command))
            self.application.add_handler(
                MessageHandler(filters.TEXT & ~filters.COMMAND, self.translate_text)
            )
                
            await self.application.initialize()
            # Set webhook URL for Telegram updates to receive messages
            await self.application.bot.set_webhook(url=f"{webhook_url}/{token}")
            logger.info(f"Webhook set to {webhook_url}/{token}")
                
            self._initialized = True
            return True
                
        except Exception as e:
            logger.error(f"Bot initialization failed: {str(e)}", exc_info=True)
            return False

    async def get_bot_status(self) -> Dict[str, Any]:
        """Gets bot status information without repeatedly calling get_me."""
        status = "inactive"
        username = self._bot_username # Use cached username
        initialized = self._initialized

        if self.application and self.application.bot and initialized:
            status = "active"
        else:
            status = "inactive"
        
        return {
            "status": status,
            "username": username,
            "initialized": initialized,
            "start_time": self._start_time.isoformat()
        }

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handles the /start command."""
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
        """Handles the /help command."""
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
        """Handles text messages for translation."""
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
            await update.message.chat.send_action("typing") # Show "typing..." status to the user
                
            result = await self.translator.translate(text)
                
            if result.success and result.translated_text:
                # Short confirmation message
                await update.message.reply_text(
                    f"{EMOJI['translate']} Bản dịch:" +
                    (f" {EMOJI['cache']}" if result.from_cache else "") # Indicate if translation came from cache
                )
                    
                # The translation itself in a separate message for easy copying
                await update.message.reply_text(result.translated_text)
                    
                logger.info(f"Translation successful for user {user.id}")
            else:
                await update.message.reply_text(
                    f"{EMOJI['error']} 申し訳ございません。\n"
                    f"{result.error_message or '翻訳エラーが発生しました。'}" # Fallback error message if no specific error
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

# Custom log filter to suppress Uvicorn's INFO logs for /health endpoint
class HealthCheckFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Check if the log is from uvicorn.access logger and if it's an INFO level GET /health 200 OK
        if record.name == "uvicorn.access" and record.levelno == logging.INFO:
            # Uvicorn's access log message format typically puts method, path, and status code in record.args
            # record.args will be a tuple like ('10.209.26.200:46028', 'GET', '/health', 'HTTP/1.1', 200)
            try:
                # Check if it's a GET request to /health and the status code is 200
                if record.args[1] == 'GET' and record.args[2] == '/health' and record.args[4] == 200:
                    return False  # Do not log this record
            except (IndexError, TypeError):
                # Handle cases where record.args might not have the expected structure
                # In such cases, we let the log pass, or log a warning for debugging the filter itself
                pass
        return True # Log all other records

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Add the custom filter to uvicorn.access logger at startup
    uvicorn_access_logger = logging.getLogger("uvicorn.access")
    health_filter = HealthCheckFilter()
    uvicorn_access_logger.addFilter(health_filter)
    logger.info("Uvicorn health check log filter added.")

    # Startup tasks: Initialize bot and translation service session
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    webhook_url = os.getenv("WEBHOOK_URL")
        
    if not token or not webhook_url:
        logger.critical("Missing required environment variables: TELEGRAM_BOT_TOKEN or WEBHOOK_URL. Exiting.")
        raise ValueError("Missing TELEGRAM_BOT_TOKEN or WEBHOOK_URL")

    # Create aiohttp session for the TranslationService at startup
    aiohttp_session = None
    try:
        aiohttp_session = aiohttp.ClientSession()
        bot.translator.set_session(aiohttp_session) # Set the session on the translator instance
        logger.info("TranslationService aiohttp session created.")

        success = await bot.initialize(token, webhook_url)
        if not success:
            logger.critical("Failed to initialize bot. Application will not start.")
            raise RuntimeError("Failed to initialize bot")
        logger.info("Bot initialized successfully and ready to receive updates.")
    except Exception as e:
        logger.critical(f"Startup failed due to bot initialization or session creation error: {e}", exc_info=True)
        # Ensure the aiohttp session is closed if any error occurs during startup
        if aiohttp_session and not aiohttp_session.closed:
            await aiohttp_session.close()
        raise # Re-raise the exception to prevent the app from starting if init fails
        
    yield # Application is running and ready to handle requests

    # Remove the custom filter at shutdown to clean up (good practice, though not strictly necessary for short-lived apps)
    uvicorn_access_logger.removeFilter(health_filter)
    logger.info("Uvicorn health check log filter removed.")

    # Shutdown tasks: Close bot and translation service sessions
    logger.info("Application shutdown initiated.")
    try:
        if bot.application:
            await bot.application.shutdown()
            logger.info("Telegram bot application shutdown complete.")
            
        if aiohttp_session and not aiohttp_session.closed: # Close the session created here
            await aiohttp_session.close()
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
        "bot_username": bot._bot_username # Include cached username in root response
    }

@app.get("/health")
async def health_check():
    """Enhanced health check endpoint for uptime monitoring."""
    current_time = datetime.utcnow()
    uptime = current_time - bot._start_time
        
    if not bot._initialized:
        # If bot is not initialized, always log at WARNING and return 503
        logger.warning(f"Health check failed: Bot not initialized. Uptime: {uptime.total_seconds()}s")
        raise HTTPException(
            status_code=503, # Service Unavailable if bot is not initialized
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

    # Determine overall status based on sub-service statuses
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
            "formatted": str(uptime).split('.')[0] # Format uptime as HH:MM:SS
        },
        "services": {
            "bot": bot_status,
            "translation": translation_status
        }
    }
    
    # Log 'ok' status at DEBUG level, 'degraded'/'error' at INFO level
    if overall_status == "ok":
        logger.debug(f"Health check: {response['status']}")
    else:
        logger.info(f"Health check: {response['status']}")
        
    return response

@app.post("/{token}")
async def telegram_webhook(token: str, request: Request):
    """Handles Telegram webhook requests."""
    if not bot._initialized:
        logger.warning("Received webhook but bot is not initialized. Responding with 503.")
        raise HTTPException(status_code=503, detail="Bot is not initialized")
        
    # Security check: Validate the incoming token against environment variable
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
