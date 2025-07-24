import asyncio
import logging
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
    ContextTypes
)

# Cấu hình logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Constants
MAX_TEXT_LENGTH = 500
RETRY_COUNT = 3
RETRY_DELAY = 1
CACHE_TIMEOUT = 3600  # 1 hour
VERSION = "1.0.1"

# Emoji Constants
EMOJI = {
    'hello': '👋',
    'translate': '🔄',
    'original': '📝',
    'warning': '⚠️',
    'info': 'ℹ️',
    'error': '❌',
    'success': '✅',
    'loading': '⏳',
    'help': '💡',
    'cache': '💾',
}

@dataclass
class CacheEntry:
    """Data class cho cache entry."""
    text: str
    timestamp: datetime
    
@dataclass
class TranslationResult:
    """Data class cho kết quả dịch."""
    original_text: str
    translated_text: Optional[str] = None
    source_lang: str = 'auto'
    target_lang: str = 'ja'
    success: bool = False
    error_message: Optional[str] = None
    from_cache: bool = False
    timestamp: str = datetime.utcnow().isoformat()

class TranslationCache:
    """Class quản lý cache cho các bản dịch."""
    
    def __init__(self, timeout: int = CACHE_TIMEOUT):
        self.cache: Dict[str, CacheEntry] = {}
        self.timeout = timeout
    
    def get(self, key: str) -> Optional[str]:
        """Lấy bản dịch từ cache."""
        if key in self.cache:
            entry = self.cache[key]
            if datetime.utcnow() - entry.timestamp < timedelta(seconds=self.timeout):
                return entry.text
            else:
                del self.cache[key]
        return None
    
    def set(self, key: str, value: str):
        """Lưu bản dịch vào cache."""
        self.cache[key] = CacheEntry(
            text=value,
            timestamp=datetime.utcnow()
        )
    
    def cleanup(self):
        """Xóa các entries hết hạn."""
        current_time = datetime.utcnow()
        expired_keys = [
            key for key, entry in self.cache.items()
            if current_time - entry.timestamp >= timedelta(seconds=self.timeout)
        ]
        for key in expired_keys:
            del self.cache[key]

class TranslationService:
    """Service class xử lý dịch thuật."""
    
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.cache = TranslationCache()
        self._setup_keigo_patterns()
        self.requests_count = 0
        self.last_cleanup = datetime.utcnow()

    def _setup_keigo_patterns(self):
        """Thiết lập patterns cho kính ngữ."""
        self.keigo_patterns = {
            # Đại từ nhân xưng
            r'\b(tôi|tao|tớ|mình)\b': '私',
            r'\b(bạn|cậu|mày)\b': 'あなた',
            r'\b(anh ấy|ông ấy)\b': '彼',
            r'\b(chị ấy|bà ấy)\b': '彼女',
            r'\b(chúng tôi|chúng ta|chúng mình)\b': '私たち',
            r'\b(họ|bọn họ)\b': '彼ら',
            
            # Từ lịch sự
            r'\b(xin|làm ơn|vui lòng)\b': 'お願いします',
            r'\b(cảm ơn)\b': 'ありがとうございます',
            r'\b(xin lỗi)\b': '申し訳ございません',
            r'\b(chào|xin chào)\b': 'こんにちは',
            
            # Trợ từ lịch sự
            r'\bhãy\b': 'ください',
            r'\b(có thể|được không)\b': 'よろしいでしょうか',
            r'\b(xin phép)\b': '失礼します',
        }

    async def ensure_session(self):
        """Đảm bảo session được khởi tạo."""
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession()
            logger.info("Created new aiohttp session")

    def preprocess_text(self, text: str) -> str:
        """Tiền xử lý văn bản."""
        # Chuẩn hóa whitespace
        text = ' '.join(text.split())
        
        # Chuẩn hóa dấu câu
        text = re.sub(r'\s+([.,!?])', r'\1', text)
        
        # Áp dụng kính ngữ patterns
        for pattern, replacement in self.keigo_patterns.items():
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        
        return text

    def postprocess_translation(self, translated: str) -> str:
        """Hậu xử lý văn bản đã dịch."""
        if not translated:
            return translated

        # Thêm trợ từ は sau chủ ngữ
        translated = re.sub(r'(私|あなた)(?!は|が)', r'\1は', translated)
        
        # Đảm bảo kết thúc câu lịch sự
        polite_endings = ['ます', 'です', 'ございます', 'でしょうか']
        if not any(translated.endswith(end) for end in polite_endings):
            if translated.endswith('。'):
                translated = translated[:-1] + 'です。'
            else:
                translated += 'です。'
        
        # Chuẩn hóa dấu chấm
        if not translated.endswith('。'):
            translated += '。'
        
        return translated

    def maybe_cleanup_cache(self):
        """Dọn dẹp cache nếu cần."""
        if datetime.utcnow() - self.last_cleanup > timedelta(hours=1):
            self.cache.cleanup()
            self.last_cleanup = datetime.utcnow()
            logger.info("Cache cleanup performed")

    async def translate(self, text: str) -> TranslationResult:
        """Dịch văn bản với cache."""
        self.maybe_cleanup_cache()
        
        # Kiểm tra cache
        cached_translation = self.cache.get(text)
        if cached_translation:
            return TranslationResult(
                original_text=text,
                translated_text=cached_translation,
                success=True,
                from_cache=True
            )
        
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
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            ),
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }

        for attempt in range(RETRY_COUNT):
            try:
                async with self.session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status == 200:
                        try:
                            data = await response.json()
                            
                            if not data or not isinstance(data, list):
                                raise ValueError("Invalid response format")
                            
                            translations = data[0] if data else []
                            if not translations or not isinstance(translations, list):
                                raise ValueError("No translation data")
                            
                            translated_parts = []
                            for item in translations:
                                if (isinstance(item, list) and 
                                    len(item) > 0 and 
                                    isinstance(item[0], str)):
                                    translated_parts.append(item[0])
                            
                            if not translated_parts:
                                raise ValueError("Empty translation")
                            
                            translated_text = ''.join(translated_parts)
                            result.translated_text = self.postprocess_translation(translated_text)
                            result.success = True
                            
                            # Lưu vào cache
                            self.cache.set(text, result.translated_text)
                            
                            logger.info(
                                f"Translation successful: {text[:30]} -> {result.translated_text[:30]}"
                            )
                            
                            return result
                            
                        except (json.JSONDecodeError, IndexError, KeyError, ValueError) as e:
                            logger.error(f"Error processing translation response: {str(e)}")
                            if attempt < RETRY_COUNT - 1:
                                await asyncio.sleep(RETRY_DELAY)
                            continue
                    else:
                        logger.error(f"API returned status {response.status}")
                        if attempt < RETRY_COUNT - 1:
                            await asyncio.sleep(RETRY_DELAY)
                        continue
                        
            except Exception as e:
                logger.error(f"Translation attempt {attempt + 1} failed: {str(e)}")
                if attempt < RETRY_COUNT - 1:
                    await asyncio.sleep(RETRY_DELAY)
                continue

        result.error_message = "Không thể dịch văn bản. Vui lòng thử lại sau."
        logger.error(f"All translation attempts failed for text: {text[:50]}")
        return result

class TelegramBot:
    """Class xử lý bot Telegram."""
    
    def __init__(self):
        self.application: Optional[Application] = None
        self.translator = TranslationService()
        self._initialized = False
        self._start_time = datetime.utcnow()

    async def initialize(self, token: str, webhook_url: str) -> bool:
        """Khởi tạo bot."""
        try:
            self.application = (
                ApplicationBuilder()
                .token(token)
                .build()
            )
            
            # Verify token
            await self.application.bot.get_me()
            
            # Đăng ký handlers
            self.application.add_handler(CommandHandler("start", self.start_command))
            self.application.add_handler(CommandHandler("help", self.help_command))
            self.application.add_handler(
                MessageHandler(filters.TEXT & ~filters.COMMAND, self.translate_text)
            )
            
            await self.application.initialize()
            await self.application.bot.set_webhook(url=f"{webhook_url}/{token}")
            
            self._initialized = True
            logger.info("Bot initialized successfully")
            return True
            
        except Exception as e:
            logger.error(f"Bot initialization failed: {str(e)}")
            return False

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Xử lý lệnh /start."""
        user = update.effective_user
        logger.info(f"User {user.id} started the bot")
        
        welcome_text = (
            f"{EMOJI['hello']} こんにちは {user.first_name}さん！\n\n"
            f"{EMOJI['info']} Bot dịch văn bản Việt-Nhật với kính ngữ tự động\n"
            f"{EMOJI['translate']} Gửi tin nhắn để nhận bản dịch\n"
            f"{EMOJI['help']} Gõ /help để xem hướng dẫn chi tiết"
        )
        await update.message.reply_text(welcome_text)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Xử lý lệnh /help."""
        logger.info(f"User {update.effective_user.id} requested help")
        
        help_text = (
            f"{EMOJI['info']} Hướng dẫn sử dụng:\n\n"
            "1. Gửi văn bản tiếng Việt để nhận bản dịch\n"
            "2. Bot sẽ tự động điều chỉnh ngữ pháp và kính ngữ\n"
            "3. /start - Bắt đầu sử dụng bot\n"
            "4. /help - Xem hướng dẫn này\n\n"
            f"{EMOJI['help']} Mẹo sử dụng:\n"
            "• Viết câu đầy đủ và rõ ràng\n"
            f"• Độ dài tối đa {MAX_TEXT_LENGTH} ký tự\n"
            "• Dùng từ ngữ lịch sự để có kính ngữ phù hợp\n"
            "• Tránh viết tắt và teen code\n\n"
            f"{EMOJI['cache']} Bot sẽ lưu cache các bản dịch\n"
            "để tăng tốc độ phản hồi"
        )
        await update.message.reply_text(help_text)

    async def translate_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Xử lý và dịch tin nhắn."""
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
                cache_indicator = f"{EMOJI['cache']} " if result.from_cache else ""
                response_text = (
                    f"{cache_indicator}{EMOJI['translate']} 日本語の翻訳:\n"
                    f"{result.translated_text}\n\n"
                    f"{EMOJI['original']} 原文:\n"
                    f"{result.original_text}"
                )
                logger.info(f"Translation successful for user {user.id}")
            else:
                response_text = (
                    f"{EMOJI['error']} 申し訳ございません。\n"
                    f"{result.error_message or '翻訳エラーが発生しました。'}"
                )
                logger.error(f"Translation failed for user {user.id}")
            
            await update.message.reply_text(response_text)
            
        except Exception as e:
            logger.error(f"Error while translating for user {user.id}: {str(e)}")
            await update.message.reply_text(
                f"{EMOJI['error']} エラーが発生しました。\n"
                "Đã xảy ra lỗi. Vui lòng thử lại sau."
            )

# Khởi tạo components
bot = TelegramBot()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan manager cho FastAPI."""
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

# Khởi tạo FastAPI app
app = FastAPI(
    title="Telegram Translation Bot",
    description="Bot dịch văn bản Việt-Nhật với kính ngữ tự động",
    version=VERSION,
    lifespan=lifespan
)

@app.get("/")
async def root():
    """Root endpoint."""
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
    """Health check endpoint."""
    if not bot._initialized:
        raise HTTPException(status_code=503, detail="Bot is not initialized")
    return {
        "status": "ok",
        "bot_status": "active",
        "version": VERSION,
        "cache_size": len(bot.translator.cache.cache)
    }

@app.post(f"/{{token}}")
async def telegram_webhook(token: str, request: Request):
    """Webhook endpoint."""
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
