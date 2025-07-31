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
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler
)
# Import for language detection library
from langdetect import detect_langs, LangDetectException

# Logging configuration
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
VERSION = "2.0.0" # Updated version for langdetect and flags

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
    'time': '⏱️',
    'detect': '🔍' # Magnifying Glass Tilted Left
}

@dataclass
class CacheEntry:
    """Represents an entry in the translation cache."""
    text: str
    timestamp: datetime

@dataclass
class TranslationResult:
    """Stores the result of a translation operation."""
    original_text: str
    translated_text: Optional[str] = None
    source_lang: Optional[str] = None
    target_lang: Optional[str] = None
    success: bool = False
    error_message: Optional[str] = None
    from_cache: bool = False
    detected_source_lang: Optional[str] = None # Stores the language detected for the input text

class TranslationCache:
    """A simple in-memory cache for translation results."""
    def __init__(self, timeout: int = CACHE_TIMEOUT):
        self.cache: Dict[str, CacheEntry] = {}
        self.timeout = timeout
        
    def get(self, key: str) -> Optional[str]:
        """Retrieves a value from the cache if it exists and is not expired."""
        if key in self.cache:
            entry = self.cache[key]
            if datetime.utcnow() - entry.timestamp < timedelta(seconds=self.timeout):
                return entry.text
            del self.cache[key]
        return None
        
    def set(self, key: str, value: str):
        """Adds or updates an entry in the cache with the current timestamp."""
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
        logger.info(f"Cache cleanup completed. Remaining entries: {len(self.cache)}")

class LangDetectService:
    """Handles language detection using the langdetect library."""
    async def detect_language(self, text: str) -> Optional[str]:
        """
        Detects the language of the given text using langdetect.
        Returns the detected language code (e.g., 'en', 'vi', 'ja') or None on failure.
        """
        try:
            detections = detect_langs(text)
            if detections:
                detected_lang = str(detections[0].lang)
                logger.info(f"Detected language for '{text[:30]}...': {detected_lang} (confidence: {detections[0].prob})")
                return detected_lang
            else:
                logger.warning(f"Langdetect returned no detections for '{text[:30]}...'")
                return None
        except LangDetectException as e:
            logger.warning(f"Could not detect language for text '{text[:30]}...' due to: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error during language detection with langdetect: {e}", exc_info=True)
            return None


class TranslationService:
    """Manages translation using MyMemory API and delegates language detection."""
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.cache = TranslationCache()
        self.last_cleanup = datetime.utcnow()
        self.mymemory_base_url = "https://api.mymemory.translated.net/get"
        self.lang_detect_service = LangDetectService() # Initialize LangDetectService
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
        """Standardizes the translated text by handling common HTML entities."""
        if not translated:
            return translated
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

    async def detect_language(self, text: str) -> Optional[str]:
        """Delegates language detection to LangDetectService."""
        return await self.lang_detect_service.detect_language(text)

    async def translate(self, text: str, source_lang: str, target_lang: str) -> TranslationResult:
        """
        Translates text using the MyMemory API.
        Args:
            text (str): The text to translate.
            source_lang (str): The language code of the source text (e.g., 'vi', 'en').
            target_lang (str): The language code of the desired target translation (e.g., 'ja', 'vi').
        """
        self.maybe_cleanup_cache()
            
        cache_key = f"{source_lang}-{target_lang}-{text}"
        cached = self.cache.get(cache_key)
        if cached:
            logger.info(f"Translation retrieved from cache for key: {cache_key[:50]}...")
            return TranslationResult(
                original_text=text,
                translated_text=cached,
                source_lang=source_lang,
                target_lang=target_lang,
                success=True,
                from_cache=True
            )
            
        if not self.session or self.session.closed:
            logger.error("TranslationService session is not active. Cannot translate.")
            return TranslationResult(
                original_text=text,
                source_lang=source_lang,
                target_lang=target_lang,
                success=False,
                error_message="Dịch vụ dịch chưa sẵn sàng."
            )

        processed_text = self.preprocess_text(text)
        result = TranslationResult(original_text=text, source_lang=source_lang, target_lang=target_lang)
            
        params = {
            "q": processed_text,
            "langpair": f"{source_lang}|{target_lang}",
            "de": "a@b.c"
        }

        for attempt in range(RETRY_COUNT):
            try:
                async with self.session.get(
                    self.mymemory_base_url,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    response.raise_for_status()
                    data = await response.json()
                            
                    if not data or "responseData" not in data:
                        raise ValueError("Invalid response format from MyMemory API.")
                            
                    translated_text = data["responseData"].get("translatedText")
                    if not translated_text:
                        raise ValueError("Empty translation received from MyMemory API.")
                            
                    result.translated_text = self.postprocess_translation(translated_text)
                    result.success = True
                            
                    self.cache.set(cache_key, result.translated_text)
                            
                    logger.info(
                        f"Translation successful (API call) {source_lang} -> {target_lang}: {text[:30]} -> "
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
    """Manages the Telegram bot's interaction logic."""
    def __init__(self):
        self.application: Optional[Application] = None
        self.translator = TranslationService()
        self._initialized = False
        self._start_time = datetime.utcnow()
        self._bot_username: Optional[str] = None

    async def initialize(self, token: str, webhook_url: str) -> bool:
        """Initializes the Telegram bot application."""
        try:
            self.application = (
                ApplicationBuilder()
                .token(token)
                .build()
            )
                
            me = await self.application.bot.get_me()
            self._bot_username = me.username
            logger.info(f"Bot info retrieved: @{self._bot_username}")
                
            self.application.add_handler(CommandHandler("start", self.start_command))
            self.application.add_handler(CommandHandler("help", self.help_command))
            self.application.add_handler(
                MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_text_and_ask_target_lang)
            )
            self.application.add_handler(
                CallbackQueryHandler(self.button_callback_handler)
            )
                
            await self.application.initialize()
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
        username = self._bot_username
        initialized = self._initialized

        if self.application and self.application.bot and initialized:
            status = "active"
        
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
            f"{EMOJI['info']} Bot dịch văn bản đa ngôn ngữ (Việt - Nhật)\n"
            f"{EMOJI['translate']} Gửi tin nhắn để bot tự động nhận diện ngôn ngữ và chọn ngôn ngữ đầu ra qua nút bấm.\n"
            f"{EMOJI['help']} Gõ /help để xem hướng dẫn"
        )
        await update.message.reply_text(welcome_text)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handles the /help command."""
        logger.info(f"User {update.effective_user.id} requested help")
            
        help_text = (
            f"{EMOJI['info']} Hướng dẫn sử dụng:\n\n"
            "1. Gửi bất kỳ văn bản nào bạn muốn dịch.\n"
            "2. Bot sẽ tự động nhận diện ngôn ngữ và hỏi bạn muốn dịch sang Tiếng Việt hay Tiếng Nhật.\n"
            "3. Nhấn vào nút ngôn ngữ bạn muốn dịch.\n"
            "4. Bản dịch sẽ được gửi.\n" # Simplified for brevity
            "5. /start - Bắt đầu sử dụng\n"
            "6. /help - Xem hướng dẫn\n\n"
            f"{EMOJI['help']} Mẹo:\n"
            "• Viết câu đầy đủ và rõ ràng\n"
            f"• Độ dài tối đa {MAX_TEXT_LENGTH} ký tự"
        )
        await update.message.reply_text(help_text)

    async def process_text_and_ask_target_lang(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handles incoming text messages.
        It detects the language and then prompts the user to select the target language
        using inline keyboard buttons.
        """
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

        logger.info(f"Processing text for language detection for user {user.id}: '{text[:50]}...'")
        await update.message.chat.send_action("typing") # Show "typing..." status to the user

        detected_lang = await self.translator.detect_language(text)

        if not detected_lang:
            await update.message.reply_text(
                f"{EMOJI['error']} Không thể nhận diện ngôn ngữ của văn bản. "
                "Vui lòng thử lại với văn bản rõ ràng hơn."
            )
            logger.warning(f"Failed to detect language for user {user.id} text: '{text[:50]}...'")
            return

        # Store the original text and detected language in user_data
        context.user_data[user.id] = {"original_text": text, "source_lang": detected_lang}
        logger.info(f"Detected language '{detected_lang}' for user {user.id}. Stored text and lang in user_data.")

        # Create inline keyboard for target language selection with flags and concise text
        keyboard = [
            [
                InlineKeyboardButton("🇻🇳 Tiếng Việt", callback_data=f"translate_to:vi"),
                InlineKeyboardButton("🇯🇵 Tiếng Nhật", callback_data=f"translate_to:ja")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Prompt for target language selection
        await update.message.reply_text(
            f"{EMOJI['translate']} Bạn muốn dịch sang ngôn ngữ nào?",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )

    async def button_callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handles inline keyboard button presses.
        Retrieves the stored text and source language, then performs the translation.
        """
        query = update.callback_query
        user = query.from_user
        await query.answer() # Acknowledge the callback query immediately

        chat_id = query.message.chat_id
        
        # Retrieve stored user data
        user_data = context.user_data.get(user.id)
        if not user_data or "original_text" not in user_data or "source_lang" not in user_data:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{EMOJI['error']} Lỗi: Không tìm thấy văn bản gốc để dịch. Vui lòng gửi lại văn bản."
            )
            logger.warning(f"User {user.id} pressed callback button but no user_data found or complete.")
            return

        original_text = user_data["original_text"]
        source_lang = user_data["source_lang"]
        
        try:
            target_lang = query.data.split(":")[1]
        except IndexError:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{EMOJI['error']} Lỗi: Dữ liệu nút bấm không hợp lệ. Vui lòng thử lại."
            )
            logger.error(f"Invalid callback_data received: {query.data}")
            return
        
        logger.info(f"User {user.id} chose to translate from {source_lang} to {target_lang} for text: '{original_text[:50]}...'")

        # Set typing status to indicate processing (still good for UX feedback)
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        
        try:
            result = await self.translator.translate(original_text, source_lang, target_lang)
                
            if result.success and result.translated_text:
                # ONLY send the translated text for easy copying/forwarding
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=result.translated_text
                )
                logger.info(f"Translation successful for user {user.id}")
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"{EMOJI['error']} 申し訳ございません。\n"
                         f"{result.error_message or '翻訳エラーが発生しました。'}"
                )
                logger.error(f"Translation failed for user {user.id}: {result.error_message}")
                
        except Exception as e:
            logger.error(f"Error during translation from button callback for user {user.id}: {e}", exc_info=True)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{EMOJI['error']} エラーが発生しました。\n"
                     "Đã xảy ra lỗi. Vui lòng thử lại sau."
            )
        finally:
            # Clean up user data after translation is attempted
            if user.id in context.user_data:
                del context.user_data[user.id]
                logger.debug(f"Cleaned up user_data for user {user.id}")

bot: Optional['TelegramBot'] = None

class HealthCheckFilter(logging.Filter):
    """
    A custom logging filter to prevent Uvicorn from logging INFO messages
    for successful /health endpoint checks, keeping logs cleaner.
    """
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "uvicorn.access" and record.levelno == logging.INFO:
            try:
                if record.args[1] == 'GET' and record.args[2] == '/health' and record.args[4] == 200:
                    return False
            except (IndexError, TypeError):
                pass
        return True

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan context manager for managing application startup and shutdown.
    Handles bot initialization, aiohttp session creation, and webhook setup.
    """
    uvicorn_access_logger = logging.getLogger("uvicorn.access")
    health_filter = HealthCheckFilter() 
    uvicorn_access_logger.addFilter(health_filter)
    logger.info("Uvicorn health check log filter added.")

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    webhook_url = os.getenv("WEBHOOK_URL")

    # Log the values of environment variables for debugging
    logger.info(f"Environment variable TELEGRAM_BOT_TOKEN: {'***' + token[-4:] if token else 'None'}")
    logger.info(f"Environment variable WEBHOOK_URL: {webhook_url or 'None'}")

    if not token or not webhook_url:
        logger.critical("Missing required environment variables: TELEGRAM_BOT_TOKEN or WEBHOOK_URL. Exiting.")
        raise ValueError("Missing TELEGRAM_BOT_TOKEN or WEBHOOK_URL")

    global bot
    bot = TelegramBot()

    aiohttp_session = None
    try:
        aiohttp_session = aiohttp.ClientSession()
        bot.translator.set_session(aiohttp_session)
        logger.info("TranslationService aiohttp session created.")

        success = await bot.initialize(token, webhook_url)
        if not success:
            logger.critical("Failed to initialize bot. Application will not start.")
            raise RuntimeError("Failed to initialize bot")
        logger.info("Bot initialized successfully and ready to receive updates.")
    except Exception as e:
        logger.critical(f"Startup failed due to bot initialization or session creation error: {e}", exc_info=True)
        if aiohttp_session and not aiohttp_session.closed:
            await aiohttp_session.close()
        raise
        
    yield

    uvicorn_access_logger.removeFilter(health_filter)
    logger.info("Uvicorn health check log filter removed.")

    logger.info("Application shutdown initiated.")
    try:
        if bot and bot.application:
            await bot.application.shutdown()
            logger.info("Telegram bot application shutdown complete.")
            
        if aiohttp_session and not aiohttp_session.closed:
            await aiohttp_session.close()
            logger.info("Translation service aiohttp session closed.")
    except Exception as e:
        logger.error(f"Error during application shutdown: {e}", exc_info=True)

app = FastAPI(
    title="Telegram Translation Bot",
    description="Bot dịch văn bản đa ngôn ngữ (Việt - Nhật)",
    version=VERSION,
    lifespan=lifespan
)

@app.get("/")
async def root():
    """Root endpoint with basic status."""
    if bot is None or not bot._initialized:
        return {
            "status": "initializing",
            "message": "Bot is still initializing. Please wait.",
            "version": VERSION,
            "timestamp": datetime.utcnow().isoformat()
        }
    
    uptime = datetime.utcnow() - bot._start_time
    cache_size = len(bot.translator.cache.cache)
    return {
        "status": "active",
        "timestamp": datetime.utcnow().isoformat(),
        "version": VERSION,
        "uptime_seconds": uptime.total_seconds(),
        "cache_entries": cache_size,
        "bot_username": bot._bot_username
    }

@app.get("/health")
async def health_check():
    """Enhanced health check endpoint for uptime monitoring."""
    current_time = datetime.utcnow()
    
    if bot is None or not bot._initialized:
        uptime = current_time - (bot._start_time if bot else current_time)
        logger.warning(f"Health check failed: Bot not initialized. Uptime: {uptime.total_seconds()}s")
        raise HTTPException(
            status_code=503,
            detail={
                "status": "error",
                "message": "Bot is not initialized",
                "timestamp": current_time.isoformat(),
                "uptime_seconds": uptime.total_seconds()
            }
        )

    uptime = current_time - bot._start_time
    bot_status = await bot.get_bot_status()
    translation_status = await bot.translator.get_service_status()

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
            "formatted": str(uptime).split('.')[0]
        },
        "services": {
            "bot": bot_status,
            "translation": translation_status
        }
    }
    
    if overall_status == "ok":
        logger.debug(f"Health check: {response['status']}")
    else:
        logger.info(f"Health check: {response['status']}")
        
    return response

@app.post("/{token}")
async def telegram_webhook(token: str, request: Request):
    """Handles Telegram webhook requests."""
    if bot is None or not bot._initialized:
        logger.warning("Received webhook but bot is not initialized. Responding with 503.")
        raise HTTPException(status_code=503, detail="Bot is not initialized")
        
    expected_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not expected_token:
        logger.error("TELEGRAM_BOT_TOKEN environment variable is not set. Cannot validate webhook.")
        raise HTTPException(status_code=500, detail="Server misconfiguration: Bot token not set.")

    if token != expected_token:
        logger.warning(f"Invalid token received in webhook URL: {token[:10]}... (expected {expected_token[:10]}...)")
        raise HTTPException(status_code=403, detail="Invalid token")
        
    try:
        update = Update.de_json(await request.json(), bot.application.bot)
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
        "bot:app",
        host="0.0.0.0",
        port=port,
        workers=1,
        reload=False,
        log_level=log_level
    )
        
    server = uvicorn.Server(config)
    server.run()
