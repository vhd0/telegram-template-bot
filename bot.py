import os
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from dataclasses import dataclass
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes, Application
)
from googletrans import Translator  # sử dụng cho dịch và detect
from langdetect import detect_langs, LangDetectException

# cấu hình logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# constants
MAX_TEXT_LENGTH = 500
CACHE_TIMEOUT = 3600

EMOJI = {
    'hello': '👋',
    'translate': '🔄',
    'warning': '⚠️',
    'info': 'ℹ️',
    'error': '❌',
    'help': '💡'
}

@dataclass
class CacheEntry:
    text: str
    timestamp: datetime

@dataclass
class TranslationResult:
    original_text: str
    translated_text: Optional[str] = None
    source_lang: Optional[str] = None
    target_lang: Optional[str] = None
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
        to_del = [k for k, v in self.cache.items()
                  if now - v.timestamp >= timedelta(seconds=self.timeout)]
        for k in to_del:
            del self.cache[k]
        logger.info(f"Cache cleaned; {len(self.cache)} entries remain.")

class LangDetectService:
    async def detect_language(self, text: str) -> Optional[str]:
        try:
            detections = detect_langs(text)
            if detections:
                return str(detections[0].lang)
        except Exception as e:
            logger.warning(f"Language detection error: {e}")
        return None

class TranslationService:
    def __init__(self):
        self.cache = TranslationCache()
        self.lang_detect_service = LangDetectService()
        self.translator = Translator()  # googletrans Translator

    async def get_service_status(self) -> Dict[str, Any]:
        return {
            "status": "active",
            "cache_size": len(self.cache.cache)
        }

    async def detect_language(self, text: str) -> Optional[str]:
        return await self.lang_detect_service.detect_language(text)

    async def translate(self, text: str, source_lang: str, target_lang: str) -> TranslationResult:
        self.cache.cleanup()
        key = f"{source_lang}-{target_lang}-{text}"
        cached = self.cache.get(key)
        if cached:
            return TranslationResult(text, translated_text=cached, source_lang=source_lang,
                                     target_lang=target_lang, success=True, from_cache=True)

        result = TranslationResult(original_text=text, source_lang=source_lang, target_lang=target_lang)
        try:
            translated = self.translator.translate(text, src=source_lang, dest=target_lang)
            result.translated_text = translated.text.strip()
            result.success = True
            self.cache.set(key, result.translated_text)
        except Exception as e:
            logger.error(f"Translation error: {e}", exc_info=True)
            result.error_message = f"Lỗi khi dịch: {e}"
        return result

class TelegramBot:
    def __init__(self):
        self.application: Optional[Application] = None
        self.translator = TranslationService()
        self._initialized = False
        self._start_time = datetime.utcnow()
        self._bot_username = None

    async def initialize(self, token: str, webhook_url: str):
        self.application = ApplicationBuilder().token(token).build()
        me = await self.application.bot.get_me()
        self._bot_username = me.username

        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_text_and_ask_target_lang))
        self.application.add_handler(CallbackQueryHandler(self.button_callback_handler))

        await self.application.initialize()
        await self.application.bot.set_webhook(url=f"{webhook_url}/{token}")
        self._initialized = True

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            f"{EMOJI['hello']} Chào bạn! Gửi tin nhắn để mình dịch nhé!"
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            f"{EMOJI['help']} Gõ /start để bắt đầu."
        )

    async def process_text_and_ask_target_lang(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if not text or len(text) > MAX_TEXT_LENGTH:
            await update.message.reply_text(f"{EMOJI['warning']} Vui lòng gửi văn bản trong giới hạn {MAX_TEXT_LENGTH} ký tự.")
            return

        await update.message.chat.send_action("typing")
        detected = await self.translator.detect_language(text)
        if not detected:
            await update.message.reply_text(f"{EMOJI['error']} Không thể nhận diện ngôn ngữ.")
            return

        context.user_data[update.effective_user.id] = {"original_text": text, "source_lang": detected}

        keyboard = [[
            InlineKeyboardButton("🇻🇳 Tiếng Việt", callback_data="translate_to:vi"),
            InlineKeyboardButton("🇯🇵 Tiếng Nhật", callback_data="translate_to:ja")
        ]]
        await update.message.reply_text(
            f"{EMOJI['translate']} Ngôn ngữ nguồn: {detected}. Chọn ngôn ngữ đích:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def button_callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = context.user_data.get(query.from_user.id)
        if not data:
            await query.message.reply_text(f"{EMOJI['error']} Không tìm thấy văn bản.")
            return

        original = data["original_text"]
        src = data["source_lang"]
        tgt = query.data.split(":")[1]

        await query.message.chat.send_action("typing")
        result = await self.translator.translate(original, src, tgt)
        if result.success:
            await query.message.reply_text(result.translated_text)
        else:
            await query.message.reply_text(f"{EMOJI['error']} {result.error_message}")

        del context.user_data[query.from_user.id]

bot = TelegramBot()

@asynccontextmanager
async def lifespan(app: FastAPI):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    webhook_url = os.getenv("WEBHOOK_URL")
    if not token or not webhook_url:
        logger.critical("Missing TELEGRAM_BOT_TOKEN or WEBHOOK_URL")
        raise RuntimeError("Missing env vars")
    await bot.initialize(token, webhook_url)
    yield
    if bot.application:
        await bot.application.shutdown()

app = FastAPI(lifespan=lifespan)

@app.post("/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != os.getenv("TELEGRAM_BOT_TOKEN"):
        raise HTTPException(status_code=403)
    update = Update.de_json(await request.json(), bot.application.bot)
    await bot.application.process_update(update)
    return {"ok": True}

@app.get("/health")
def health_check():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}

@app.get("/")
def root():
    uptime = datetime.utcnow() - bot._start_time
    return {"status": "active", "uptime_seconds": uptime.total_seconds()}
