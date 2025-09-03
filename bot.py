import asyncio
import logging
from logging.handlers import RotatingFileHandler
import os
import json
import aiohttp
import urllib.parse
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Tuple
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
    ContextTypes,
    CallbackQueryHandler
)
from langdetect import detect_langs, LangDetectException

# Simple console logging for Render deployment
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Constants
MAX_TEXT_LENGTH = 500
RETRY_COUNT = 3
RETRY_DELAY = 1
CACHE_TIMEOUT = 3600  # 1 hour
VERSION = "2.3.0"  # Updated version with optimizations
ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")
DEPLOYMENT_TIME = "2025-09-03 02:45:09"  # Current deployment timestamp
DEPLOYMENT_USER = "vhd0"  # Current deployment user

# Emoji map for messages
EMOJI = {
    'hello': '👋', 'translate': '🔄', 'warning': '⚠️', 'info': 'ℹ️',
    'error': '❌', 'success': '✅', 'help': '💡', 'cache': '💾',
    'time': '⏱️', 'detect': '🔍'
}

@dataclass(slots=True)
class CacheEntry:
    """Represents an entry in the translation cache."""
    text: str
    timestamp: datetime

@dataclass(slots=True)
class TranslationResult:
    """Stores the result of a translation operation."""
    original_text: str
    translated_text: Optional[str] = None
    source_lang: Optional[str] = None
    target_lang: Optional[str] = None
    success: bool = False
    error_message: Optional[str] = None
    from_cache: bool = False
    detected_source_lang: Optional[str] = None

class TranslationCache:
    """Optimized in-memory cache for translation results."""
    def __init__(self, timeout: int = CACHE_TIMEOUT, max_size: int = 10000):
        self.cache: Dict[str, CacheEntry] = {}
        self.timeout = timeout
        self.max_size = max_size
        self._last_cleanup = datetime.utcnow()
        self._cleanup_interval = timedelta(minutes=30)

    def get(self, key: str) -> Optional[str]:
        """Get cached translation with optimized expiry check."""
        if key in self.cache:
            entry = self.cache[key]
            if datetime.utcnow() - entry.timestamp < timedelta(seconds=self.timeout):
                return entry.text
            del self.cache[key]
        return None

    def set(self, key: str, value: str) -> None:
        """Set cache entry with size management."""
        if len(self.cache) >= self.max_size:
            # Remove oldest 10% of entries if cache is full
            sorted_entries = sorted(
                self.cache.items(),
                key=lambda x: x[1].timestamp
            )
            for old_key, _ in sorted_entries[:len(sorted_entries) // 10]:
                del self.cache[old_key]
        self.cache[key] = CacheEntry(text=value, timestamp=datetime.utcnow())

    def maybe_cleanup(self) -> None:
        """Optimized cache cleanup with reduced frequency."""
        now = datetime.utcnow()
        if now - self._last_cleanup >= self._cleanup_interval:
            self._last_cleanup = now
            expired = [
                k for k, v in self.cache.items()
                if now - v.timestamp >= timedelta(seconds=self.timeout)
            ]
            for k in expired:
                del self.cache[k]

class LangDetectService:
    """Optimized language detection service with caching."""
    def __init__(self, cache_timeout: int = 3600):
        self._cache: Dict[str, Tuple[str, datetime]] = {}
        self._cache_timeout = timedelta(seconds=cache_timeout)

    async def detect_language(self, text: str) -> Optional[str]:
        """Detect language with caching and optimized processing."""
        cache_key = hash(text[:100])
        cached = self._cache.get(str(cache_key))
        
        if cached and (datetime.utcnow() - cached[1]) < self._cache_timeout:
            return cached[0]

        try:
            detections = await asyncio.to_thread(detect_langs, text)
            if detections and detections[0].prob > 0.5:
                lang = str(detections[0].lang)
                self._cache[str(cache_key)] = (lang, datetime.utcnow())
                return lang
        except LangDetectException:
            pass
        except Exception as e:
            logger.error(f"Language detection error: {e}", exc_info=True)
        return None

class TranslationService:
    """Optimized translation service with improved error handling and caching."""
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.cache = TranslationCache()
        self.last_cleanup = datetime.utcnow()
        self.mymemory_base_url = "https://api.mymemory.translated.net/get"
        self.lang_detect_service = LangDetectService()
        self._initialized = False
        self._rate_limit_reset = datetime.utcnow()
        self._requests_remaining = 100

    def set_session(self, session: aiohttp.ClientSession) -> None:
        """Configure translation service with optimized session settings."""
        self.session = session
        self._initialized = True
        logger.info("TranslationService initialized with optimized session")

    async def get_service_status(self) -> Dict[str, Any]:
        """Get service status with minimal overhead."""
        return {
            "status": "active" if self._initialized and self.session and not self.session.closed else "inactive",
            "cache_size": len(self.cache.cache),
            "rate_limit_reset": self._rate_limit_reset.isoformat(),
            "requests_remaining": self._requests_remaining
        }

    async def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str
    ) -> TranslationResult:
        """Optimized translation method with improved error handling and rate limiting."""
        self.cache.maybe_cleanup()

        if self._requests_remaining <= 0 and datetime.utcnow() < self._rate_limit_reset:
            return TranslationResult(
                original_text=text,
                error_message="Rate limit exceeded. Please try again later.",
                success=False
            )

        cache_key = f"{source_lang}-{target_lang}-{text}"
        cached = self.cache.get(cache_key)
        if cached:
            return TranslationResult(
                original_text=text,
                translated_text=cached,
                source_lang=source_lang,
                target_lang=target_lang,
                success=True,
                from_cache=True
            )

        if not self.session or self.session.closed:
            return TranslationResult(
                original_text=text,
                error_message="Translation service unavailable",
                success=False
            )

        result = TranslationResult(
            original_text=text,
            source_lang=source_lang,
            target_lang=target_lang
        )

        params = {
            "q": text.strip(),
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
                    self._requests_remaining = int(response.headers.get('X-RateLimit-Remaining', 100))
                    reset_after = int(response.headers.get('X-RateLimit-Reset', 3600))
                    self._rate_limit_reset = datetime.utcnow() + timedelta(seconds=reset_after)

                    data = await response.json()
                    if "responseData" in data and data["responseData"].get("translatedText"):
                        result.translated_text = data["responseData"]["translatedText"]
                        result.success = True
                        self.cache.set(cache_key, result.translated_text)
                        return result

                    raise ValueError(f"Invalid API response: {data}")

            except aiohttp.ClientError as e:
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                result.error_message = f"Connection error: {str(e)}"
            except Exception as e:
                logger.error(f"Translation error: {e}", exc_info=True)
                result.error_message = "Translation service error"
                break

        return result

class TelegramBot:
    """Optimized Telegram bot with improved message handling."""
    def __init__(self):
        self.application: Optional[Application] = None
        self.translator = TranslationService()
        self._initialized = False
        self._start_time = datetime.utcnow()
        self._bot_username: Optional[str] = None
        self._health_status = {"status": "initializing"}
        self._last_status_update = datetime.utcnow()
        self._status_update_interval = timedelta(minutes=1)

    async def initialize(self, token: str, webhook_url: str) -> bool:
        """Initialize bot with optimized startup sequence."""
        try:
            self.application = (
                ApplicationBuilder()
                .token(token)
                .concurrent_updates(True)
                .connection_pool_size(100)
                .connect_timeout(10.0)
                .pool_timeout(10.0)
                .read_timeout(10.0)
                .write_timeout(10.0)
                .build()
            )

            me = await self.application.bot.get_me()
            self._bot_username = me.username

            self.application.add_handler(CommandHandler("start", self.start_command))
            self.application.add_handler(CommandHandler("help", self.help_command))
            self.application.add_handler(
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    self.process_text_and_translate
                )
            )
            self.application.add_handler(CallbackQueryHandler(self.button_callback_handler))

            await self.application.initialize()
            await self.application.bot.set_webhook(url=f"{webhook_url}/{token}")

            self._initialized = True
            logger.info(f"Bot @{self._bot_username} initialized successfully")
            return True

        except Exception as e:
            logger.error(f"Bot initialization failed: {e}", exc_info=True)
            return False

    async def get_bot_status(self) -> Dict[str, Any]:
        """Get cached bot status with periodic updates."""
        now = datetime.utcnow()
        if now - self._last_status_update >= self._status_update_interval:
            self._health_status.update({
                "status": "active" if self._initialized else "inactive",
                "username": self._bot_username,
                "uptime": (now - self._start_time).total_seconds(),
                "initialized": self._initialized
            })
            self._last_status_update = now
        return self._health_status

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command with optimized message."""
        user = update.effective_user
        logger.info(f"User {user.id} started bot")
        
        welcome_text = (
            f"{EMOJI['hello']} Xin chào {user.first_name}!\n\n"
            f"{EMOJI['info']} Bot dịch văn bản tự động sang tiếng Nhật.\n"
            f"{EMOJI['translate']} Gửi tin nhắn để dịch.\n"
            f"{EMOJI['help']} Gõ /help để xem hướng dẫn"
        )
        await update.message.reply_text(welcome_text)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command with optimized message."""
        help_text = (
            f"{EMOJI['info']} Hướng dẫn sử dụng:\n\n"
            "1. Gửi văn bản cần dịch\n"
            "2. Bot sẽ tự động dịch sang tiếng Nhật\n\n"
            "Lệnh:\n"
            "• /start - Bắt đầu\n"
            "• /help - Hướng dẫn\n\n"
            f"{EMOJI['help']} Lưu ý: Tối đa {MAX_TEXT_LENGTH} ký tự"
        )
        await update.message.reply_text(help_text)

    async def process_text_and_translate(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Process and translate text with optimized handling."""
        user = update.effective_user
        text = update.message.text.strip()

        if not text:
            await update.message.reply_text(f"{EMOJI['warning']} Vui lòng nhập văn bản.")
            return

        if len(text) > MAX_TEXT_LENGTH:
            await update.message.reply_text(
                f"{EMOJI['warning']} Văn bản quá dài (>{MAX_TEXT_LENGTH} ký tự)."
            )
            return

        await update.message.chat.send_action("typing")

        try:
            detected_lang = await self.translator.lang_detect_service.detect_language(text)
            if not detected_lang:
                await update.message.reply_text(
                    f"{EMOJI['error']} Không nhận diện được ngôn ngữ."
                )
                return

            result = await self.translator.translate(text, detected_lang, 'ja')

            if result.success and result.translated_text:
                await update.message.reply_text(result.translated_text)
                logger.info(f"Translation successful for user {user.id}")
            else:
                error_msg = result.error_message or "Lỗi dịch thuật"
                await update.message.reply_text(f"{EMOJI['error']} {error_msg}")
                logger.warning(f"Translation failed for user {user.id}: {error_msg}")

        except Exception as e:
            logger.error(f"Translation error for user {user.id}: {e}", exc_info=True)
            await update.message.reply_text(f"{EMOJI['error']} Đã xảy ra lỗi.")

    async def button_callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle button callbacks."""
        query = update.callback_query
        await query.answer(
            "Vui lòng gửi văn bản trực tiếp.",
            show_alert=True
        )

# Global variables
bot: Optional[TelegramBot] = None
bot_init_task: Optional[asyncio.Task] = None
is_bot_ready = False

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan with optimized startup/shutdown."""
    global bot, bot_init_task, is_bot_ready

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    webhook_url = os.environ.get("WEBHOOK_URL")

    if not token or not webhook_url:
        raise RuntimeError("Missing required environment variables")

    try:
        # Configure optimized aiohttp session
        timeout = aiohttp.ClientTimeout(total=10, connect=3)
        connector = aiohttp.TCPConnector(
            limit=100,
            ttl_dns_cache=300,
            use_dns_cache=True
        )
        session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            raise_for_status=True
        )

        bot = TelegramBot()
        bot.translator.set_session(session)

        bot_init_task = asyncio.create_task(bot.initialize(token, webhook_url))
        is_bot_ready = True

        yield

    finally:
        if bot_init_task and not bot_init_task.done():
            bot_init_task.cancel()
            try:
                await bot_init_task
            except asyncio.CancelledError:
                pass

        if bot:
            if bot.application:
                await bot.application.shutdown()
            if bot.translator.session and not bot.translator.session.closed:
                await bot.translator.session.close()

# FastAPI application
app = FastAPI(
    title="Telegram Translation Bot",
    description="Bot dịch văn bản tự động sang tiếng Nhật",
    version=VERSION,
    lifespan=lifespan,
    docs_url=None if ENVIRONMENT == "production" else "/docs",
    redoc_url=None if ENVIRONMENT == "production" else "/redoc"
)

@app.get("/")
async def root():
    """Root endpoint with deployment info."""
    if not is_bot_ready or not bot:
        return {
            "status": "initializing",
            "version": VERSION,
            "deployment_time": DEPLOYMENT_TIME,
            "deployment_user": DEPLOYMENT_USER,
            "timestamp": datetime.utcnow().isoformat()
        }

    return {
        "status": "active",
        "version": VERSION,
        "deployment_time": DEPLOYMENT_TIME,
        "deployment_user": DEPLOYMENT_USER,
        "uptime": (datetime.utcnow() - bot._start_time).total_seconds()
    }

@app.get("/health")
async def health_check():
    """Optimized health check endpoint."""
    if not is_bot_ready or not bot:
        raise HTTPException(status_code=503, detail="Bot initializing")

    status = await bot.get_bot_status()
    translation_status = await bot.translator.get_service_status()

    return {
        "status": "ok" if status["status"] == "active" and
                        translation_status["status"] == "active" else "degraded",
        "version": VERSION,
        "services": {
            "bot": status,
            "translation": translation_status
        }
    }

@app.post("/{token}")
async def telegram_webhook(token: str, request: Request):
    """Handle Telegram webhooks with improved error handling."""
    if not is_bot_ready or not bot:
        raise HTTPException(status_code=503, detail="Bot not ready")

    if token != os.environ.get("TELEGRAM_BOT_TOKEN"):
        raise HTTPException(status_code=403, detail="Invalid token")

    try:
        update = Update.de_json(await request.json(), bot.application.bot)
        await bot.application.process_update(update)
        return {"status": "ok"}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error")

if __name__ == "__main__":
    import uvicorn

    uvicorn_config = uvicorn.Config(
        "bot:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        workers=2,
        loop="uvloop",
        http="httptools",
        log_level="info",
        reload=False,
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips="*"
    )

    server = uvicorn.Server(uvicorn_config)
    server.run()
