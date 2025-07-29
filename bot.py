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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler
)

# Cấu hình ghi log
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Hằng số
MAX_TEXT_LENGTH = 500
RETRY_COUNT = 3
RETRY_DELAY = 1
CACHE_TIMEOUT = 3600  # 1 giờ
VERSION = "2.0.0" # Phiên bản mới với logic chọn ngôn ngữ đầu vào/đầu ra

# Các tùy chọn ngôn ngữ cho dịch thuật động
# Ánh xạ mã ngôn ngữ (được MyMemory API sử dụng) với tên hiển thị
LANG_OPTIONS = {
    "vi": "Tiếng Việt",
    "ja": "Tiếng Nhật",
    "en": "Tiếng Anh"
}

# Ánh xạ Emoji cho các tin nhắn
# Đây là các emoji Unicode tiêu chuẩn, được chọn vì khả năng tương thích đa nền tảng.
# Hình ảnh hiển thị có thể hơi khác nhau giữa các thiết bị/hệ điều hành
# do các triển khai font emoji khác nhau của nhà cung cấp nền tảng.
EMOJI = {
    'hello': '👋',     # Hand Wave
    'translate': '🔄', # Counterclockwise Arrows Button
    'warning': '⚠️',    # Warning Sign
    'info': 'ℹ️',      # Information Sign
    'error': '❌',     # Cross Mark
    'success': '✅',    # White Heavy Check Mark
    'help': '💡',      # Light Bulb
    'cache': '💾',     # Floppy Disk (biểu tượng chung cho việc lưu/cache)
    'time': '⏱️',       # Stopwatch (biểu tượng chung cho thời gian/thời lượng)
    'input_lang': '💬', # Speech Balloon for input language
    'output_lang': '➡️' # Right Arrow for output language
}

# Trạng thái người dùng để quản lý luồng hội thoại
class UserState:
    SELECTING_INPUT_LANG = "SELECTING_INPUT_LANG"
    AWAITING_TEXT_INPUT = "AWAITING_TEXT_INPUT"
    SELECTING_OUTPUT_LANG = "SELECTING_OUTPUT_LANG"
    IDLE = "IDLE"

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
        """Xóa các mục hết hạn khỏi bộ đệm."""
        now = datetime.utcnow()
        expired = [
            k for k, v in self.cache.items()
            if now - v.timestamp >= timedelta(seconds=self.timeout)
        ]
        for k in expired:
            del self.cache[k]
        logger.info(f"Dọn dẹp bộ đệm hoàn tất. Số mục còn lại: {len(self.cache)}")

class TranslationService:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None 
        self.cache = TranslationCache()
        self.last_cleanup = datetime.utcnow()
        self.base_url = "https://api.mymemory.translated.net/get"
        self._initialized = False

    def set_session(self, session: aiohttp.ClientSession):
        """Đặt phiên aiohttp client cho dịch vụ dịch."""
        self.session = session
        self._initialized = True
        logger.info("Phiên aiohttp ClientSession đã được đặt cho TranslationService.")

    def preprocess_text(self, text: str) -> str:
        """Chuẩn hóa văn bản để dịch."""
        return text.strip()

    def postprocess_translation(self, translated: str) -> str:
        """Chuẩn hóa văn bản đã dịch."""
        if not translated:
            return translated
            
        # Xử lý các thực thể HTML phổ biến
        translated = translated.replace("&quot;", '"')
        translated = translated.replace("&#39;", "'")
        translated = translated.replace("&amp;", "&")
        translated = translated.replace("&lt;", "<")
        translated = translated.replace("&gt;", ">")
            
        return translated.strip()

    def maybe_cleanup_cache(self):
        """Kích hoạt dọn dẹp bộ đệm nếu đủ thời gian đã trôi qua."""
        if datetime.utcnow() - self.last_cleanup > timedelta(hours=1):
            self.cache.cleanup()
            self.last_cleanup = datetime.utcnow()
            logger.info("Dọn dẹp bộ đệm theo lịch trình đã thực hiện.")

    async def get_service_status(self) -> Dict[str, Any]:
        """Lấy trạng thái của dịch vụ dịch."""
        return {
            "status": "active" if self._initialized and self.session and not self.session.closed else "inactive",
            "cache_size": len(self.cache.cache),
            "last_cleanup": self.last_cleanup.isoformat()
        }

    async def translate(self, text: str, source_lang: str, target_lang: str) -> TranslationResult:
        """
        Dịch văn bản bằng MyMemory API với ngôn ngữ nguồn và đích đã biết.
        """
        self.maybe_cleanup_cache()

        processed_text = self.preprocess_text(text)
        result = TranslationResult(original_text=text)

        if not self.session or self.session.closed:
            logger.error("Phiên TranslationService không hoạt động. Không thể tiến hành dịch.")
            return TranslationResult(
                original_text=text,
                success=False,
                error_message="Dịch vụ dịch chưa sẵn sàng."
            )

        # Kiểm tra nếu ngôn ngữ nguồn và đích giống nhau
        if source_lang == target_lang and source_lang in LANG_OPTIONS:
            logger.info(f"Ngôn ngữ nguồn ({source_lang}) giống ngôn ngữ đích. Sử dụng văn bản gốc làm bản dịch.")
            return TranslationResult(
                original_text=text,
                translated_text=self.postprocess_translation(processed_text),
                success=True,
                from_cache=False
            )
        
        # Tạo khóa bộ đệm với ngôn ngữ nguồn và đích cụ thể
        cache_key = f"{source_lang}_{processed_text}|{target_lang}"

        # Kiểm tra bộ đệm trước khi gọi API
        cached = self.cache.get(cache_key)
        if cached:
            logger.info(f"Bản dịch được lấy từ bộ đệm cho văn bản: {processed_text[:30]}... ({source_lang} -> {target_lang})")
            return TranslationResult(
                original_text=text,
                translated_text=cached,
                success=True,
                from_cache=True
            )

        # Thực hiện dịch thuật qua API
        langpair = f"{source_lang}|{target_lang}"
        params = {
            "q": processed_text,
            "langpair": langpair,
            "de": "a@b.c" # Email để có giới hạn tốc độ tốt hơn
        }

        for attempt in range(RETRY_COUNT):
            try:
                async with self.session.get(
                    self.base_url,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    response.raise_for_status()
                    data = await response.json()

                    if not data or "responseData" not in data or not data["responseData"].get("translatedText"):
                        raise ValueError("Phản hồi dịch thuật không hợp lệ hoặc trống.")

                    translated_text = data["responseData"].get("translatedText")

                    result.translated_text = self.postprocess_translation(translated_text)
                    result.success = True
                    self.cache.set(cache_key, result.translated_text)
                    logger.info(f"Dịch thuật thành công (cuộc gọi API): {processed_text[:30]} -> {result.translated_text[:30]} ({source_lang} -> {target_lang})")
                    return result

            except aiohttp.ClientError as e:
                logger.error(f"Lỗi Client HTTP trong quá trình dịch (lần thử {attempt + 1}/{RETRY_COUNT}): {e}", exc_info=True)
                result.error_message = f"Lỗi kết nối dịch vụ dịch: {e}"
            except (json.JSONDecodeError, ValueError) as e:
                logger.error(f"Lỗi dữ liệu dịch cuối cùng (lần thử {attempt + 1}/{RETRY_COUNT}): {e}", exc_info=True)
                result.error_message = f"Lỗi dữ liệu dịch: {e}"
            except Exception as e:
                logger.error(f"Lỗi không mong muốn trong quá trình dịch (lần thử {attempt + 1}/{RETRY_COUNT}): {e}", exc_info=True)
                result.error_message = "Đã xảy ra lỗi không xác định khi dịch."

            if attempt < RETRY_COUNT - 1:
                await asyncio.sleep(RETRY_DELAY)
            else:
                logger.error(f"Tất cả {RETRY_COUNT} lần thử dịch cuối cùng đã thất bại cho văn bản: {text[:50]}... ({source_lang} -> {target_lang})")

        result.error_message = result.error_message or "Không thể dịch văn bản. Vui lòng thử lại sau."
        return result

class TelegramBot:
    def __init__(self):
        self.application: Optional[Application] = None
        self.translator = TranslationService()
        self._initialized = False
        self._start_time = datetime.utcnow()
        self._bot_username: Optional[str] = None
        # Quản lý trạng thái và dữ liệu tạm thời cho mỗi người dùng
        self.user_states: Dict[int, str] = {}
        self.user_data: Dict[int, Dict[str, Any]] = {}

    async def initialize(self, token: str, webhook_url: str) -> bool:
        """Khởi tạo ứng dụng Telegram bot."""
        try:
            self.application = (
                ApplicationBuilder()
                .token(token)
                .build()
            )
                
            me = await self.application.bot.get_me()
            self._bot_username = me.username
            logger.info(f"Thông tin bot đã lấy: @{self._bot_username}")
                
            # Đăng ký các handler cho lệnh và tin nhắn văn bản
            self.application.add_handler(CommandHandler("start", self.start_command))
            self.application.add_handler(CommandHandler("help", self.help_command))
            
            # Handler cho tin nhắn văn bản (chỉ xử lý khi bot đang chờ văn bản)
            self.application.add_handler(
                MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text_input)
            )
            
            # Handler cho các nút inline query
            # Xử lý cả việc chọn ngôn ngữ đầu vào và đầu ra
            self.application.add_handler(
                CallbackQueryHandler(self.handle_language_selection_callback, pattern=r"^lang_")
            )
                
            await self.application.initialize()
            await self.application.bot.set_webhook(url=f"{webhook_url}/{token}")
            logger.info(f"Webhook được đặt thành {webhook_url}/{token}")
                
            self._initialized = True
            return True
                
        except Exception as e:
            logger.error(f"Khởi tạo bot thất bại: {str(e)}", exc_info=True)
            return False

    async def get_bot_status(self) -> Dict[str, Any]:
        """Lấy thông tin trạng thái bot mà không gọi get_me nhiều lần."""
        status = "inactive"
        username = self._bot_username
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

    # --- Các Handler mới cho luồng đa ngôn ngữ ---

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Xử lý lệnh /start và yêu cầu chọn ngôn ngữ đầu vào."""
        user_id = update.effective_user.id
        user_first_name = update.effective_user.first_name
        logger.info(f"Người dùng {user_id} đã khởi động bot.")
            
        welcome_text = (
            f"{EMOJI['hello']} こんにちは {user_first_name}さん！\n\n"
            f"{EMOJI['info']} Bot dịch văn bản đa ngôn ngữ.\n"
            f"{EMOJI['input_lang']} Vui lòng chọn ngôn ngữ bạn sẽ nhập vào."
        )
        
        # Đặt trạng thái người dùng thành chờ chọn ngôn ngữ đầu vào
        self.user_states[user_id] = UserState.SELECTING_INPUT_LANG
        self.user_data[user_id] = {} # Xóa dữ liệu cũ của người dùng

        # Tạo các nút inline để chọn ngôn ngữ đầu vào
        keyboard = []
        for lang_code, lang_name in LANG_OPTIONS.items():
            # callback_data: "lang_input_<language_code>"
            keyboard.append([InlineKeyboardButton(lang_name, callback_data=f"lang_input_{lang_code}")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(welcome_text, reply_markup=reply_markup)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Xử lý lệnh /help."""
        logger.info(f"Người dùng {update.effective_user.id} đã yêu cầu trợ giúp.")
            
        help_text = (
            f"{EMOJI['info']} Hướng dẫn sử dụng:\n\n"
            f"1. Gõ /start để bắt đầu.\n"
            f"2. {EMOJI['input_lang']} Chọn ngôn ngữ bạn muốn nhập vào.\n"
            f"3. Gửi văn bản của bạn.\n"
            f"4. {EMOJI['output_lang']} Chọn ngôn ngữ bạn muốn dịch ra.\n"
            f"5. Nhận bản dịch.\n\n"
            f"{EMOJI['help']} Mẹo:\n"
            "• Viết câu đầy đủ và rõ ràng để dịch tốt nhất.\n"
            f"• Độ dài tối đa {MAX_TEXT_LENGTH} ký tự."
        )
        await update.message.reply_text(help_text)

    async def handle_language_selection_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Xử lý việc nhấn nút inline để chọn ngôn ngữ (đầu vào hoặc đầu ra).
        """
        query = update.callback_query
        user_id = query.from_user.id
        
        await query.answer() # Luôn trả lời callback query để loại bỏ animation

        callback_data = query.data
        parts = callback_data.split('_') # e.g., ["lang", "input", "vi"] or ["lang", "output", "ja"]
        
        if len(parts) != 3 or parts[0] != "lang":
            logger.warning(f"Dữ liệu callback không hợp lệ nhận được: {callback_data}")
            await query.edit_message_text(f"{EMOJI['warning']} Lựa chọn không hợp lệ. Vui lòng thử lại.")
            self.user_states[user_id] = UserState.IDLE # Đặt lại trạng thái
            return

        lang_type = parts[1] # "input" or "output"
        lang_code = parts[2] # e.g., "vi", "ja", "en"
        lang_name = LANG_OPTIONS.get(lang_code, "ngôn ngữ không xác định")

        if lang_type == "input":
            logger.info(f"Người dùng {user_id} đã chọn ngôn ngữ đầu vào: {lang_name} ({lang_code})")
            self.user_data[user_id]['source_language'] = lang_code
            self.user_states[user_id] = UserState.AWAITING_TEXT_INPUT

            await query.edit_message_text(
                f"{EMOJI['success']} Bạn đã chọn ngôn ngữ đầu vào là **{lang_name}**.\n"
                f"{EMOJI['translate']} Bây giờ, hãy gửi văn bản bạn muốn dịch."
            )
        elif lang_type == "output":
            logger.info(f"Người dùng {user_id} đã chọn ngôn ngữ đầu ra: {lang_name} ({lang_code})")
            self.user_data[user_id]['target_language'] = lang_code
            self.user_states[user_id] = UserState.IDLE # Hoàn tất quá trình lựa chọn ngôn ngữ, sẵn sàng dịch

            text_to_translate = self.user_data[user_id].get('pending_translation_text')
            source_lang = self.user_data[user_id].get('source_language')

            if not text_to_translate or not source_lang:
                await query.edit_message_text(f"{EMOJI['warning']} Đã xảy ra lỗi. Không tìm thấy văn bản hoặc ngôn ngữ nguồn. Vui lòng bắt đầu lại với /start.")
                logger.error(f"Dữ liệu dịch thiếu cho người dùng {user_id} sau khi chọn ngôn ngữ đầu ra. Text: {text_to_translate is not None}, Source: {source_lang is not None}")
                self.user_states[user_id] = UserState.IDLE
                self.user_data[user_id] = {}
                return
            
            # Cập nhật tin nhắn để báo hiệu đang dịch
            await query.edit_message_text(
                f"{EMOJI['translate']} Đang dịch từ {LANG_OPTIONS.get(source_lang, source_lang)} sang {lang_name}..."
            )

            # Tiến hành dịch
            await self._perform_translation(
                user_id,
                query.message, # Sử dụng query.message để trả lời trong cùng cuộc trò chuyện
                text_to_translate,
                source_lang,
                lang_code
            )
            self.user_data[user_id] = {} # Xóa dữ liệu tạm sau khi dịch xong
        else:
            logger.warning(f"Loại ngôn ngữ không xác định trong callback: {lang_type}")
            await query.edit_message_text(f"{EMOJI['warning']} Lựa chọn không xác định. Vui lòng thử lại.")
            self.user_states[user_id] = UserState.IDLE

    async def handle_text_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Xử lý văn bản người dùng nhập vào.
        Nếu trạng thái đang chờ văn bản, nó sẽ lưu văn bản và yêu cầu chọn ngôn ngữ đầu ra.
        """
        user_id = update.effective_user.id
        text = update.message.text.strip()

        # Kiểm tra độ dài văn bản
        if not text:
            await update.message.reply_text(f"{EMOJI['warning']} Vui lòng nhập văn bản để dịch.")
            return

        if len(text) > MAX_TEXT_LENGTH:
            await update.message.reply_text(
                f"{EMOJI['warning']} Văn bản quá dài. "
                f"Vui lòng giữ dưới {MAX_TEXT_LENGTH} ký tự."
            )
            return

        current_state = self.user_states.get(user_id, UserState.IDLE)
        
        if current_state == UserState.AWAITING_TEXT_INPUT:
            source_lang = self.user_data[user_id].get('source_language')
            if not source_lang:
                logger.error(f"Người dùng {user_id} ở trạng thái AWAITING_TEXT_INPUT nhưng thiếu source_language.")
                await update.message.reply_text(f"{EMOJI['error']} Đã xảy ra lỗi. Vui lòng bắt đầu lại với /start.")
                self.user_states[user_id] = UserState.IDLE
                self.user_data[user_id] = {}
                return

            logger.info(f"Người dùng {user_id} đã gửi văn bản để dịch: '{text[:50]}...' (ngôn ngữ nguồn: {source_lang})")
            
            # Lưu văn bản và đặt trạng thái chờ chọn ngôn ngữ đầu ra
            self.user_data[user_id]['pending_translation_text'] = text
            self.user_states[user_id] = UserState.SELECTING_OUTPUT_LANG

            # Tạo các nút inline để chọn ngôn ngữ đầu ra
            keyboard = []
            for lang_code, lang_name in LANG_OPTIONS.items():
                # callback_data: "lang_output_<language_code>"
                keyboard.append([InlineKeyboardButton(lang_name, callback_data=f"lang_output_{lang_code}")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"{EMOJI['output_lang']} Bạn muốn dịch sang ngôn ngữ nào?",
                reply_markup=reply_markup
            )
        else:
            # Nếu người dùng gửi văn bản không đúng lúc, yêu cầu họ bắt đầu lại
            await update.message.reply_text(
                f"{EMOJI['info']} Tôi không hiểu. Vui lòng bắt đầu lại bằng cách gõ /start để chọn ngôn ngữ bạn muốn nhập trước."
            )
            logger.info(f"Người dùng {user_id} gửi văn bản không đúng luồng. Trạng thái hiện tại: {current_state}")
            self.user_states[user_id] = UserState.IDLE # Đặt lại trạng thái
            self.user_data[user_id] = {} # Xóa dữ liệu tạm

    async def _perform_translation(self, user_id: int, message_obj, text: str, source_lang: str, target_lang: str):
        """Hàm trợ giúp để thực hiện dịch và gửi kết quả."""
        try:
            await message_obj.chat.send_action("typing")
                
            result = await self.translator.translate(text, source_lang, target_lang)
                
            if result.success and result.translated_text:
                # Gửi tin nhắn xác nhận
                await message_obj.reply_text(
                    f"{EMOJI['translate']} Bản dịch từ {LANG_OPTIONS.get(source_lang, source_lang)} sang {LANG_OPTIONS.get(target_lang, target_lang)}:" +
                    (f" {EMOJI['cache']}" if result.from_cache else "")
                )
                    
                # Gửi văn bản đã dịch
                await message_obj.reply_text(result.translated_text)
                    
                logger.info(f"Dịch thuật thành công cho người dùng {user_id}: {source_lang} -> {target_lang}")
            else:
                await message_obj.reply_text(
                    f"{EMOJI['error']} Xin lỗi, không thể dịch.\n"
                    f"{result.error_message or 'Đã xảy ra lỗi khi dịch.'}"
                )
                logger.error(f"Dịch thuật thất bại cho người dùng {user_id}: {result.error_message}")
                
        except Exception as e:
            logger.error(f"Lỗi trong quá trình dịch cho người dùng {user_id}: {e}", exc_info=True)
            await message_obj.reply_text(
                f"{EMOJI['error']} Đã xảy ra lỗi. Vui lòng thử lại sau."
            )

# Khởi tạo instance bot toàn cục
bot = TelegramBot()

# Bộ lọc log tùy chỉnh để chặn các log INFO của Uvicorn cho endpoint /health
class HealthCheckFilter(logging.Filter):
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
    # Thêm bộ lọc tùy chỉnh vào logger uvicorn.access khi khởi động
    uvicorn_access_logger = logging.getLogger("uvicorn.access")
    health_filter = HealthCheckFilter()
    uvicorn_access_logger.addFilter(health_filter)
    logger.info("Bộ lọc log health check của Uvicorn đã được thêm.")

    # Các tác vụ khởi động: Khởi tạo bot và phiên dịch vụ dịch
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    webhook_url = os.getenv("WEBHOOK_URL")
        
    if not token or not webhook_url:
        logger.critical("Thiếu biến môi trường bắt buộc: TELEGRAM_BOT_TOKEN hoặc WEBHOOK_URL. Đang thoát.")
        raise ValueError("Thiếu TELEGRAM_BOT_TOKEN hoặc WEBHOOK_URL")

    # Tạo phiên aiohttp cho TranslationService khi khởi động
    aiohttp_session = None
    try:
        aiohttp_session = aiohttp.ClientSession()
        bot.translator.set_session(aiohttp_session)
        logger.info("Phiên aiohttp TranslationService đã được tạo.")

        success = await bot.initialize(token, webhook_url)
        if not success:
            logger.critical("Không thể khởi tạo bot. Ứng dụng sẽ không khởi động.")
            raise RuntimeError("Không thể khởi tạo bot")
        logger.info("Bot đã được khởi tạo thành công và sẵn sàng nhận cập nhật.")
    except Exception as e:
        logger.critical(f"Khởi động thất bại do lỗi khởi tạo bot hoặc tạo phiên: {e}", exc_info=True)
        if aiohttp_session and not aiohttp_session.closed:
            await aiohttp_session.close()
        raise
        
    yield # Ứng dụng đang chạy và sẵn sàng xử lý yêu cầu

    # Xóa bộ lọc tùy chỉnh khi tắt để dọn dẹp
    uvicorn_access_logger.removeFilter(health_filter)
    logger.info("Bộ lọc log health check của Uvicorn đã được xóa.")

    # Các tác vụ tắt: Đóng phiên bot và dịch vụ dịch
    logger.info("Đã bắt đầu tắt ứng dụng.")
    try:
        if bot.application:
            await bot.application.shutdown()
            logger.info("Ứng dụng Telegram bot đã tắt hoàn toàn.")
            
        if aiohttp_session and not aiohttp_session.closed:
            await aiohttp_session.close()
            logger.info("Phiên aiohttp dịch vụ dịch đã đóng.")
    except Exception as e:
        logger.error(f"Lỗi trong quá trình tắt ứng dụng: {e}", exc_info=True)

# Khởi tạo ứng dụng FastAPI
app = FastAPI(
    title="Telegram Translation Bot",
    description="Bot dịch văn bản Việt-Nhật",
    version=VERSION,
    lifespan=lifespan
)

@app.get("/")
async def root():
    """Endpoint gốc với trạng thái cơ bản."""
    uptime = datetime.utcnow() - bot._start_time
    cache_size = len(bot.translator.cache.cache) 
    return {
        "status": "active" if bot._initialized else "initializing",
        "timestamp": datetime.utcnow().isoformat(),
        "version": VERSION,
        "uptime_seconds": uptime.total_seconds(),
        "cache_entries": cache_size,
        "bot_username": bot._bot_username
    }

@app.get("/health")
async def health_check():
    """Endpoint kiểm tra sức khỏe nâng cao để giám sát thời gian hoạt động."""
    current_time = datetime.utcnow()
    uptime = current_time - bot._start_time
        
    if not bot._initialized:
        logger.warning(f"Kiểm tra sức khỏe thất bại: Bot chưa được khởi tạo. Thời gian hoạt động: {uptime.total_seconds()}s")
        raise HTTPException(
            status_code=503,
            detail={
                "status": "error",
                "message": "Bot chưa được khởi tạo",
                "timestamp": current_time.isoformat(),
                "uptime_seconds": uptime.total_seconds()
            }
        )

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
        logger.debug(f"Kiểm tra sức khỏe: {response['status']}")
    else:
        logger.info(f"Kiểm tra sức khỏe: {response['status']}")
        
    return response

@app.post("/{token}")
async def telegram_webhook(token: str, request: Request):
    """Xử lý các yêu cầu webhook của Telegram."""
    if not bot._initialized:
        logger.warning("Đã nhận webhook nhưng bot chưa được khởi tạo. Trả về 503.")
        raise HTTPException(status_code=503, detail="Bot chưa được khởi tạo")
        
    expected_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not expected_token:
        logger.error("Biến môi trường TELEGRAM_BOT_TOKEN chưa được đặt. Không thể xác thực webhook.")
        raise HTTPException(status_code=500, detail="Cấu hình máy chủ sai: Bot token chưa được đặt.")

    if token != expected_token:
        logger.warning(f"Đã nhận token không hợp lệ trong URL webhook: {token[:10]}... (dự kiến {expected_token[:10]}...)")
        raise HTTPException(status_code=403, detail="Token không hợp lệ")
        
    try:
        update = Update.de_json(await request.json(), bot.application.bot)
        await bot.application.process_update(update)
        return {"status": "ok"}
    except json.JSONDecodeError:
        logger.error("Đã nhận webhook với tải trọng JSON không hợp lệ.")
        raise HTTPException(status_code=400, detail="Tải trọng JSON không hợp lệ.")
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Lỗi khi xử lý cập nhật Telegram: {error_msg}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Lỗi khi xử lý cập nhật: {error_msg}")

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
