import asyncio
import logging
import os
import json
import aiohttp
import urllib.parse
import re
from datetime import datetime, timedelta
from typing import Optional, Dict
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
VERSION = "1.1.0"

# Emoji map
EMOJI = {
    'hello': '👋',
    'translate': '🔄',
    'warning': '⚠️',
    'info': 'ℹ️',
    'error': '❌',
    'success': '✅',
    'help': '💡',
    'cache': '💾'
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
            if datetime.utcnow() - entry.timestamp < timedelta(seconds=self.timeout):
                return entry.text
            del self.cache[key]
        return None
        
    def set(self, key: str, value: str):
        self.cache[key] = CacheEntry(text=value, timestamp=datetime.utcnow())
        
    def cleanup(self):
        now = datetime.utcnow()
        expired = [
            k for k, v in self.cache.items()
            if now - v.timestamp >= timedelta(seconds=self.timeout)
        ]
        for k in expired:
            del self.cache[k]

class TranslationService:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.cache = TranslationCache()
        self.last_cleanup = datetime.utcnow()
        self.base_url = "https://api.mymemory.translated.net/get"

    async def ensure_session(self):
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession()

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
        if datetime.utcnow() - self.last_cleanup > timedelta(hours=1):
            self.cache.cleanup()
            self.last_cleanup = datetime.utcnow()

    async def translate(self, text: str) -> TranslationResult:
        """Dịch văn bản sử dụng MyMemory API."""
        self.maybe_cleanup_cache()
        
        # Check cache
        cached = self.cache.get(text)
        if cached:
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
            "de": "a@b.c"  # Email for better rate limits
        }

        # Try translation with retries
        for attempt in range(RETRY_COUNT):
            try:
                async with self.session.get(
                    self.base_url,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        
                        if not data or "responseData" not in data:
                            raise ValueError("Invalid response format")
                        
                        translated_text = data["responseData"].get("translatedText")
                        if not translated_text:
                            raise ValueError("Empty translation")
                        
                        # Process the translation
                        result.translated_text = self.postprocess_translation(translated_text)
                        result.success = True
                        
                        # Cache successful translation
                        self.cache.set(text, result.translated_text)
                        
                        logger.info(
                            f"Translation successful: {text[:30]} -> "
                            f"{result.translated_text[:30]}"
                        )
                        
                        return result
                    else:
                        raise ValueError(f"API returned status {response.status}")
                        
            except Exception as e:
                logger.error(f"Translation attempt {attempt + 1} failed: {str(e)}")
                if attempt < RETRY_COUNT - 1:
                    await asyncio.sleep(RETRY_DELAY)
                continue

        result.error_message = "Không thể dịch văn bản. Vui lòng thử lại sau."
        return result

class TelegramBot:
    def __init__(self):
        self.application: Optional[Application] = None
        self.translator = TranslationService()
        self._initialized = False
        self._start_time = datetime.utcnow()

    async def initialize(self, token: str, webhook_url: str) -> bool:
        try:
            self.application = (
                ApplicationBuilder()
                .token(token)
                .build()
            )
            
            await self.application.bot.get_me()
            
            # Register handlers
            self.application.add_handler(CommandHandler("start", self.start_command))
            self.application.add_handler(CommandHandler("help", self.help_command))
            self.application.add_handler(
                MessageHandler(filters.TEXT & ~filters.COMMAND, self.translate_text)
            )
            
            await self.application.initialize()
            await self.application.bot.set_webhook(url=f"{webhook_url}/{token}")
            
            self._initialized = True
            return True
            
        except Exception as e:
            logger.error(f"Bot initialization failed: {str(e)}")
            return False

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

        logger.info(f"Translating for user {user.id}: {text[:50]}...")
        
        try:
            await update.message.chat.send_action("typing")
            
            result = await self.translator.translate(text)
            
            if result.success and result.translated_text:
                # Tin nhắn thông báo ngắn gọn
                await update.message.reply_text(
                    f"{EMOJI['translate']} Bản dịch:" +
                    (f" {EMOJI['cache']}" if result.from_cache else "")
                )
                
                # Bản dịch trong tin nhắn riêng
                await update.message.reply_text(result.translated_text)
                
                logger.info(f"Translation successful for user {user.id}")
            else:
                await update.message.reply_text(
                    f"{EMOJI['error']} 申し訳ございません。\n"
                    f"{result.error_message or '翻訳エラーが発生しました。'}"
                )
                logger.error(f"Translation failed for user {user.id}")
            
        except Exception as e:
            logger.error(f"Error while translating for user {user.id}: {str(e)}")
            await update.message.reply_text(
                f"{EMOJI['error']} エラーが発生しました。\n"
                "Đã xảy ra lỗi. Vui lòng thử lại sau."
            )

# Initialize bot
bot = TelegramBot()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    webhook_url = os.getenv("WEBHOOK_URL")
    
    if not token or not webhook_url:
        logger.error("Missing required environment variables")
        raise ValueError("Missing TELEGRAM_BOT_TOKEN or WEBHOOK_URL")
    
    try:
        success = await bot.initialize(token, webhook_url)
        if not success:
            raise ValueError("Failed to initialize bot")
        logger.info("Bot initialized successfully")
    except Exception as e:
        logger.error(f"Startup failed: {str(e)}")
        raise
    
    yield
    
    # Shutdown
    try:
        if bot.application:
            await bot.application.shutdown()
            logger.info("Bot shutdown complete")
        
        if bot.translator.session:
            await bot.translator.session.close()
            logger.info("Translation service shutdown complete")
    except Exception as e:
        logger.error(f"Shutdown error: {str(e)}")

# Initialize FastAPI
app = FastAPI(
    title="Telegram Translation Bot",
    description="Bot dịch văn bản Việt-Nhật",
    version=VERSION,
    lifespan=lifespan
)

@app.get("/")
async def root():
    uptime = datetime.utcnow() - bot._start_time
    cache_size = len(bot.translator.cache.cache)
    return {
        "status": "active" if bot._initialized else "initializing",
        "timestamp": datetime.utcnow().isoformat(),
        "version": VERSION,
        "uptime_seconds": uptime.total_seconds(),
        "cache_entries": cache_size
    }

@app.get("/health")
async def health_check():
    if not bot._initialized:
        raise HTTPException(status_code=503, detail="Bot is not initialized")
    return {
        "status": "ok",
        "bot_status": "active",
        "version": VERSION,
        "cache_size": len(bot.translator.cache.cache)
    }

@app.post("/{token}")
async def telegram_webhook(token: str, request: Request):
    if not bot._initialized:
        raise HTTPException(status_code=503, detail="Bot is not initialized")
    
    if token != os.getenv("TELEGRAM_BOT_TOKEN"):
        logger.warning(f"Invalid token received: {token[:10]}...")
        raise HTTPException(status_code=403, detail="Invalid token")
    
    try:
        update = Update.de_json(await request.json(), bot.application.bot)
        await bot.application.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error processing update: {error_msg}")
        raise HTTPException(status_code=500, detail=error_msg)

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
