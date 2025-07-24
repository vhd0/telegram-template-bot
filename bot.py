import asyncio
import logging
import os
import json
import aiohttp
import urllib.parse
import re
from datetime import datetime
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
VERSION = "1.0.0"

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
}

@dataclass
class TranslationResult:
    """Data class cho kết quả dịch."""
    original_text: str
    translated_text: Optional[str] = None
    source_lang: str = 'auto'
    target_lang: str = 'ja'
    success: bool = False
    error_message: Optional[str] = None
    timestamp: str = datetime.utcnow().isoformat()

class TranslationService:
    """Service class xử lý dịch thuật."""
    
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.keigo_patterns = {
            # Đại từ nhân xưng
            r'\b(tôi|tao|tớ|mình)\b': '私',
            r'\b(bạn|cậu|mày)\b': 'あなた',
            r'\b(anh ấy|ông ấy)\b': '彼',
            r'\b(chị ấy|bà ấy)\b': '彼女',
            
            # Từ lịch sự
            r'\b(xin|làm ơn|vui lòng)\b': 'お願いします',
            r'\b(cảm ơn)\b': 'ありがとうございます',
            r'\b(xin lỗi)\b': '申し訳ございません',
            
            # Trợ từ lịch sự
            r'\bhãy\b': 'ください',
            r'\b(có thể|được không)\b': 'よろしいでしょうか',
        }

    async def ensure_session(self):
        """Đảm bảo session được khởi tạo."""
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession()

    def preprocess_text(self, text: str) -> str:
        """Tiền xử lý văn bản."""
        text = ' '.join(text.split())
        text = re.sub(r'\s+([.,!?])', r'\1', text)
        
        for pattern, replacement in self.keigo_patterns.items():
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        
        return text

    def postprocess_translation(self, translated: str) -> str:
        """Hậu xử lý văn bản đã dịch."""
        if not translated:
            return translated

        translated = re.sub(r'(私|あなた)(?!は|が)', r'\1は', translated)
        
        polite_endings = ['ます', 'です', 'ございます']
        if not any(translated.endswith(end) for end in polite_endings):
            if translated.endswith('。'):
                translated = translated[:-1] + 'です。'
            else:
                translated += 'です。'
        
        if not translated.endswith('。'):
            translated += '。'
        
        return translated

    async def translate(self, text: str) -> TranslationResult:
        """Dịch văn bản."""
        await self.ensure_session()
        
        processed_text = self.preprocess_text(text)
        encoded_text = urllib.parse.quote(processed_text)
        
        result = TranslationResult(original_text=text)
        
        url = (
            "https://translate.googleapis.com/translate_a/single"
            "?client=gtx&sl=auto&tl=ja&dt=t&dt=rm&dt=bd"
            f"&q={encoded_text}"
        )

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }

        for attempt in range(RETRY_COUNT):
            try:
                async with self.session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data and isinstance(data, list) and len(data) > 0:
                            translated_parts = [
                                part[0] for part in data[0]
                                if part and isinstance(part, list) and len(part) > 0
                            ]
                            
                            if translated_parts:
                                result.translated_text = self.postprocess_translation(
                                    ''.join(translated_parts)
                                )
                                result.success = True
                                return result
                            
            except Exception as e:
                logger.error(f"Translation attempt {attempt + 1} failed: {str(e)}")
                if attempt < RETRY_COUNT - 1:
                    await asyncio.sleep(RETRY_DELAY)
                continue

        result.error_message = "Không thể dịch văn bản. Vui lòng thử lại sau."
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
            f"{EMOJI['info']} Bot dịch văn bản Việt-Nhật\n"
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
            "• Tránh viết tắt và teen code"
        )
        await update.message.reply_text(help_text)

    async def translate_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Xử lý và dịch tin nhắn."""
        user = update.effective_user
        text = update.message.text.strip()
        
        if not text:
            await update.message.reply_text(f"{EMOJI['warning']} Vui lòng nhập văn bản để dịch.")
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
                response_text = (
                    f"{EMOJI['translate']} 日本語の翻訳:\n"
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
    return {
        "status": "active" if bot._initialized else "initializing",
        "timestamp": datetime.utcnow().isoformat(),
        "version": VERSION,
        "uptime_seconds": uptime.total_seconds()
    }

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    if not bot._initialized:
        raise HTTPException(status_code=503, detail="Bot is not initialized")
    return {
        "status": "ok",
        "bot_status": "active",
        "version": VERSION
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
