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
RETRY_DELAY = 1  # seconds

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
    """Data class để lưu trữ kết quả dịch."""
    original_text: str
    translated_text: Optional[str]
    source_lang: str = 'auto'
    target_lang: str = 'ja'
    success: bool = False
    error_message: Optional[str] = None

class TranslationService:
    """Service class để xử lý dịch thuật."""
    
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self._setup_keigo_patterns()

    def _setup_keigo_patterns(self):
        """Thiết lập patterns cho kính ngữ."""
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
        """Tiền xử lý văn bản trước khi dịch."""
        # Chuẩn hóa khoảng trắng và dấu câu
        text = ' '.join(text.split())
        text = re.sub(r'\s+([.,!?])', r'\1', text)
        
        # Áp dụng kính ngữ patterns
        for pattern, replacement in self.keigo_patterns.items():
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        
        return text

    def postprocess_translation(self, translated: str) -> str:
        """Hậu xử lý văn bản sau khi dịch."""
        # Thêm trợ từ は sau chủ ngữ
        translated = re.sub(r'(私|あなた)(?!は|が)', r'\1は', translated)
        
        # Đảm bảo kết thúc câu lịch sự
        polite_endings = ['ます', 'です', 'ございます']
        if not any(translated.endswith(end) for end in polite_endings):
            if translated.endswith('。'):
                translated = translated[:-1] + 'です。'
            else:
                translated += 'です。'
        
        # Chuẩn hóa dấu chấm
        if not translated.endswith('。'):
            translated += '。'
        
        return translated

    async def translate(self, text: str) -> TranslationResult:
        """Dịch văn bản với Google Translate."""
        await self.ensure_session()
        
        # Tiền xử lý văn bản
        processed_text = self.preprocess_text(text)
        encoded_text = urllib.parse.quote(processed_text)
        
        # Xây dựng URL
        url = (
            "https://translate.googleapis.com/translate_a/single"
            "?client=gtx"
            "&sl=auto"
            "&tl=ja"
            "&dt=t"
            "&dt=rm"
            "&dt=bd"
            f"&q={encoded_text}"
        )

        # Thử dịch với số lần retry
        for attempt in range(RETRY_COUNT):
            try:
                async with self.session.get(
                    url,
                    headers={'User-Agent': 'Mozilla/5.0'},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data and isinstance(data, list) and len(data) > 0:
                            translated_parts = []
                            for part in data[0]:
                                if part and isinstance(part, list) and len(part) > 0:
                                    translated_parts.append(part[0])
                            
                            translated_text = ''.join(translated_parts)
                            translated_text = self.postprocess_translation(translated_text)
                            
                            return TranslationResult(
                                original_text=text,
                                translated_text=translated_text,
                                success=True
                            )
            except Exception as e:
                logger.error(f"Translation attempt {attempt + 1} failed: {str(e)}")
                if attempt < RETRY_COUNT - 1:
                    await asyncio.sleep(RETRY_DELAY)
                continue

        return TranslationResult(
            original_text=text,
            success=False,
            error_message="Có lỗi xảy ra khi dịch văn bản"
        )

class TelegramBot:
    """Class chính để xử lý bot Telegram."""
    
    def __init__(self):
        self.application: Optional[Application] = None
        self.translator = TranslationService()

    async def initialize(self, token: str, webhook_url: str):
        """Khởi tạo bot."""
        self.application = (
            ApplicationBuilder()
            .token(token)
            .build()
        )
        
        # Đăng ký handlers
        self.register_handlers()
        
        # Khởi tạo webhook
        await self.application.initialize()
        await self.application.bot.set_webhook(url=f"{webhook_url}/{token}")
        logger.info(f"Webhook set up at {webhook_url}/{token}")

    def register_handlers(self):
        """Đăng ký các handlers cho bot."""
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            self.translate_text
        ))

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
        """Xử lý và dịch tin nhắn văn bản."""
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
            # Hiển thị đang typing
            await update.message.chat.send_action("typing")
            
            # Thực hiện dịch
            result = await self.translator.translate(text)
            
            if result.success:
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
                    f"{result.error_message}"
                )
                logger.error(f"Translation failed for user {user.id}")
            
            await update.message.reply_text(response_text)
            
        except Exception as e:
            logger.error(f"Error while translating for user {user.id}: {str(e)}")
            await update.message.reply_text(
                f"{EMOJI['error']} エラーが発生しました。\n"
                "Đã xảy ra lỗi. Vui lòng thử lại sau."
            )

# Khởi tạo FastAPI app
app = FastAPI()
bot = TelegramBot()

@app.on_event("startup")
async def startup_event():
    """Khởi tạo ứng dụng khi startup."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    webhook_url = os.getenv("WEBHOOK_URL")
    
    if not token or not webhook_url:
        raise ValueError("Missing required environment variables")
        
    try:
        await bot.initialize(token, webhook_url)
        logger.info("Bot initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize bot: {str(e)}")
        raise

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup khi shutdown."""
    if bot.application:
        await bot.application.shutdown()
        logger.info("Bot shutdown complete")
    
    if bot.translator.session:
        await bot.translator.session.close()
        logger.info("Translation service shutdown complete")

@app.get("/")
async def root():
    """Root endpoint cho health check."""
    return {
        "status": "active",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "1.0.0"
    }

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok"}

@app.post(f"/{{token}}")
async def telegram_webhook(token: str, request: Request):
    """Webhook endpoint cho Telegram updates."""
    if token != os.getenv("TELEGRAM_BOT_TOKEN"):
        logger.warning(f"Invalid token received: {token[:10]}...")
        raise HTTPException(status_code=403, detail="Invalid token")
    
    try:
        update = Update.de_json(await request.json(), bot.application.bot)
        await bot.application.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error processing update: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run(
        "bot:app",
        host="0.0.0.0",
        port=port,
        workers=1,
        reload=False
    )
