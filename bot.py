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
VERSION = "1.0.3"

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
    source_lang: str = 'auto'
    target_lang: str = 'ja'
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
        self.keigo_patterns = {
            r'\b(xin|làm ơn|vui lòng)\b': 'お願いします',
            r'\b(cảm ơn)\b': 'ありがとうございます',
            r'\b(xin lỗi)\b': '申し訳ございません'
        }
        self.last_cleanup = datetime.utcnow()

    async def ensure_session(self):
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession()

    def preprocess_text(self, text: str) -> str:
        text = ' '.join(text.split())
        text = re.sub(r'\s+([.,!?])', r'\1', text)
        
        for pattern, replacement in self.keigo_patterns.items():
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        
        return text

    def postprocess_translation(self, translated: str) -> str:
        if not translated:
            return translated

        polite_endings = ['ます', 'です', 'ございます', 'でしょうか']
        if not any(translated.endswith(end) for end in polite_endings):
            translated = f"{translated.rstrip('。')}です。"

        return translated

    def maybe_cleanup_cache(self):
        if datetime.utcnow() - self.last_cleanup > timedelta(hours=1):
            self.cache.cleanup()
            self.last_cleanup = datetime.utcnow()

    async def translate(self, text: str) -> TranslationResult:
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
        encoded_text = urllib.parse.quote(processed_text)
        
        result = TranslationResult(original_text=text)
        
        url = (
            "https://translate.googleapis.com/translate_a/single"
            "?client=gtx&sl=auto&tl=ja&dt=t"
            f"&q={encoded_text}"
        )

        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'application/json'
        }

        # Try translation with retries
        for attempt in range(RETRY_COUNT):
            try:
                async with self.session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        
                        if not data or not isinstance(data, list):
                            raise ValueError("Invalid response format")
                        
                        translations = data[0] if data else []
                        if not translations:
                            raise ValueError("No translation data")
                        
                        translated_text = ''
                        for item in translations:
                            if (isinstance(item, list) and 
                                len(item) > 0 and 
                                isinstance(item[0], str)):
                                translated_text += item[0]
                        
                        if not translated_text:
                            raise ValueError("Empty translation")
                        
                        result.translated_text = self.postprocess_translation(translated_text)
                        result.success = True
                        
                        # Cache successful translation
                        self.cache.set(text, result.translated_text)
                        
                        return result
                        
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
            "2. Bot sẽ tự động điều chỉnh kính ngữ\n"
            "3. Bản dịch sẽ được gửi riêng để dễ copy\n"
            "4. /start - Bắt đầu sử dụng\n"
            "5. /help - Xem hướng dẫn\n\n"
            f"{EMOJI['help']} Mẹo:\n"
            "• Viết câu đầy đủ và rõ ràng\n"
            f"• Độ dài tối đa {MAX_TEXT_LENGTH} ký tự\n"
            "• Dùng từ ngữ lịch sự"
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
    description="Bot dịch văn bản Việt-Nhật với kính ngữ tự động",
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
