import asyncio
import logging
import os
import json
import aiohttp
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from dataclasses import dataclass
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
)

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────
MAX_TEXT_LENGTH   = 5000
RETRY_COUNT       = 3
RETRY_DELAY       = 1       # giây
CACHE_TIMEOUT     = 3600    # 1 giờ
KEEPALIVE_INTERVAL = 14 * 60  # 14 phút — dưới ngưỡng spin-down 15 phút của Render
BOT_INIT_TIMEOUT  = 30      # giây chờ tối đa khi webhook đến trong lúc khởi động
VERSION           = "3.1.0"
ENVIRONMENT       = os.environ.get("ENVIRONMENT", "development")

def _deepl_base_url(api_key: str) -> str:
    return (
        "https://api-free.deepl.com/v2"
        if api_key.endswith(":fx")
        else "https://api.deepl.com/v2"
    )

EMOJI = {
    "hello": "👋", "translate": "🔄", "warning": "⚠️", "info": "ℹ️",
    "error": "❌", "success": "✅", "help": "💡", "cache": "💾",
    "time": "⏱️", "quota": "📊", "ping": "🏓",
}

# ─────────────────────────────────────────────
# Cache
# ─────────────────────────────────────────────
@dataclass(slots=True)
class CacheEntry:
    text: str
    timestamp: datetime


class TranslationCache:
    def __init__(self, timeout: int = CACHE_TIMEOUT, max_size: int = 10_000):
        self._cache: Dict[str, CacheEntry] = {}
        self.timeout = timeout
        self.max_size = max_size
        self._last_cleanup = datetime.utcnow()
        self._cleanup_interval = timedelta(minutes=30)

    def _is_valid(self, entry: CacheEntry) -> bool:
        return (datetime.utcnow() - entry.timestamp).total_seconds() < self.timeout

    def get(self, key: str) -> Optional[str]:
        entry = self._cache.get(key)
        if entry:
            if self._is_valid(entry):
                return entry.text
            del self._cache[key]
        return None

    def set(self, key: str, value: str) -> None:
        if len(self._cache) >= self.max_size:
            oldest = sorted(self._cache.items(), key=lambda x: x[1].timestamp)
            for k, _ in oldest[: len(oldest) // 10]:
                del self._cache[k]
        self._cache[key] = CacheEntry(text=value, timestamp=datetime.utcnow())

    def maybe_cleanup(self) -> None:
        now = datetime.utcnow()
        if now - self._last_cleanup >= self._cleanup_interval:
            self._last_cleanup = now
            expired = [k for k, v in self._cache.items() if not self._is_valid(v)]
            for k in expired:
                del self._cache[k]
            if expired:
                logger.debug(f"Cache: dọn {len(expired)} bản dịch hết hạn")

    @property
    def size(self) -> int:
        return len(self._cache)


# ─────────────────────────────────────────────
# Translation result
# ─────────────────────────────────────────────
@dataclass
class TranslationResult:
    original_text: str
    translated_text: Optional[str] = None
    detected_source_lang: Optional[str] = None
    success: bool = False
    error_message: Optional[str] = None
    from_cache: bool = False


# ─────────────────────────────────────────────
# DeepL Service
# ─────────────────────────────────────────────
class DeepLService:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = _deepl_base_url(api_key)
        self.session: Optional[aiohttp.ClientSession] = None
        self.cache = TranslationCache()
        self._initialized = False

    def set_session(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self._initialized = True
        logger.info(f"DeepLService khởi động — endpoint: {self.base_url}")

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"DeepL-Auth-Key {self.api_key}",
            "Content-Type": "application/json",
        }

    async def get_usage(self) -> Dict[str, Any]:
        if not self.session or self.session.closed:
            return {"error": "Session unavailable"}
        try:
            async with self.session.get(
                f"{self.base_url}/usage",
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                data = await resp.json()
                used  = data.get("character_count", 0)
                limit = data.get("character_limit", 0)
                return {
                    "used": used,
                    "limit": limit,
                    "remaining": limit - used,
                    "percent_used": round(used / limit * 100, 1) if limit else 0,
                }
        except Exception as e:
            logger.error(f"DeepL usage error: {e}")
            return {"error": str(e)}

    async def get_service_status(self) -> Dict[str, Any]:
        ok = self._initialized and self.session and not self.session.closed
        return {
            "status": "active" if ok else "inactive",
            "cache_size": self.cache.size,
            "endpoint": self.base_url,
        }

    async def translate(
        self,
        text: str,
        target_lang: str = "JA",
        source_lang: Optional[str] = None,
    ) -> TranslationResult:
        self.cache.maybe_cleanup()

        cache_key = f"deepl:{source_lang or 'auto'}:{target_lang}:{text}"
        cached = self.cache.get(cache_key)
        if cached:
            return TranslationResult(
                original_text=text,
                translated_text=cached,
                success=True,
                from_cache=True,
            )

        if not self.session or self.session.closed:
            return TranslationResult(
                original_text=text,
                error_message="Dịch vụ tạm thời không khả dụng.",
                success=False,
            )

        payload: Dict[str, Any] = {
            "text": [text.strip()],
            "target_lang": target_lang.upper(),
        }
        if source_lang:
            payload["source_lang"] = source_lang.upper()

        result = TranslationResult(original_text=text)

        for attempt in range(RETRY_COUNT):
            try:
                async with self.session.post(
                    f"{self.base_url}/translate",
                    headers=self._headers(),
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 456:
                        result.error_message = "Đã hết quota DeepL tháng này."
                        return result
                    if resp.status == 429:
                        wait = RETRY_DELAY * (2 ** attempt)
                        logger.warning(f"DeepL rate limit, thử lại sau {wait}s")
                        await asyncio.sleep(wait)
                        continue
                    if resp.status >= 400:
                        body = await resp.text()
                        result.error_message = f"DeepL lỗi {resp.status}: {body[:200]}"
                        return result

                    data = await resp.json()
                    translation = data.get("translations", [{}])[0]
                    translated  = translation.get("text", "").strip()

                    if not translated:
                        result.error_message = "DeepL trả về kết quả rỗng."
                        return result

                    result.translated_text        = translated
                    result.detected_source_lang   = translation.get("detected_source_language")
                    result.success                = True
                    self.cache.set(cache_key, translated)
                    return result

            except aiohttp.ClientError as e:
                logger.warning(f"DeepL request lỗi (lần {attempt + 1}): {e}")
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                result.error_message = "Lỗi kết nối đến DeepL."
            except Exception as e:
                logger.error(f"DeepL unexpected error: {e}", exc_info=True)
                result.error_message = "Lỗi dịch thuật không xác định."
                break

        return result


# ─────────────────────────────────────────────
# Keep-Alive Service
# ─────────────────────────────────────────────
class KeepAliveService:
    """
    Tự ping endpoint /ping mỗi 14 phút để ngăn Render
    spin-down service sau 15 phút không hoạt động.
    """

    def __init__(self, ping_url: str, session: aiohttp.ClientSession):
        self.ping_url = ping_url
        self.session  = session
        self._task: Optional[asyncio.Task] = None
        self._ping_count  = 0
        self._last_ping: Optional[datetime] = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="keepalive")
        logger.info(f"KeepAlive bắt đầu — ping mỗi {KEEPALIVE_INTERVAL // 60} phút → {self.ping_url}")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        # Chờ 60 giây sau khi khởi động để bot kịp init xong
        await asyncio.sleep(60)
        while True:
            try:
                async with self.session.get(
                    self.ping_url,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    self._ping_count += 1
                    self._last_ping   = datetime.utcnow()
                    logger.info(f"KeepAlive ping #{self._ping_count}: HTTP {resp.status}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"KeepAlive ping thất bại: {e}")
            await asyncio.sleep(KEEPALIVE_INTERVAL)

    @property
    def stats(self) -> Dict[str, Any]:
        return {
            "ping_count": self._ping_count,
            "last_ping":  self._last_ping.isoformat() if self._last_ping else None,
            "interval_minutes": KEEPALIVE_INTERVAL // 60,
        }


# ─────────────────────────────────────────────
# Telegram Bot
# ─────────────────────────────────────────────
class TelegramBot:
    def __init__(self, deepl_api_key: str):
        self.application: Optional[Application] = None
        self.translator  = DeepLService(deepl_api_key)
        self._initialized = False
        self._start_time  = datetime.utcnow()
        self._bot_username: Optional[str] = None
        self._health_status: Dict[str, Any] = {"status": "initializing"}
        self._last_status_update = datetime.utcnow()
        self._status_update_interval = timedelta(minutes=1)

    # ── Init ─────────────────────────────────
    async def initialize(self, token: str, webhook_url: str) -> bool:
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

            self.application.add_handler(CommandHandler("start",  self.start_command))
            self.application.add_handler(CommandHandler("help",   self.help_command))
            self.application.add_handler(CommandHandler("status", self.status_command))
            self.application.add_handler(
                MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_translation)
            )
            self.application.add_handler(CallbackQueryHandler(self.button_callback_handler))

            await self.application.initialize()
            await self.application.bot.set_webhook(url=f"{webhook_url}/{token}")

            self._initialized = True
            logger.info(f"Bot @{self._bot_username} sẵn sàng ✅")
            return True

        except Exception as e:
            logger.error(f"Khởi tạo bot thất bại: {e}", exc_info=True)
            return False

    # ── Status ───────────────────────────────
    async def get_bot_status(self) -> Dict[str, Any]:
        now = datetime.utcnow()
        if now - self._last_status_update >= self._status_update_interval:
            self._health_status = {
                "status":          "active" if self._initialized else "initializing",
                "username":        self._bot_username,
                "uptime_seconds":  (now - self._start_time).total_seconds(),
                "initialized":     self._initialized,
            }
            self._last_status_update = now
        return self._health_status

    # ── Commands ─────────────────────────────
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        logger.info(f"User {user.id} gõ /start")
        await update.message.reply_text(
            f"{EMOJI['hello']} Xin chào {user.first_name}!\n\n"
            f"{EMOJI['translate']} Bot dịch tự động sang tiếng Nhật (powered by DeepL).\n"
            f"{EMOJI['info']} Gửi bất kỳ văn bản nào để dịch.\n"
            f"{EMOJI['help']} Gõ /help để xem hướng dẫn."
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            f"{EMOJI['info']} Hướng dẫn sử dụng:\n\n"
            "• Gửi văn bản bằng bất kỳ ngôn ngữ nào\n"
            "• Bot tự nhận diện và dịch sang tiếng Nhật 🇯🇵\n\n"
            "Lệnh:\n"
            "  /start  — Bắt đầu\n"
            "  /help   — Hướng dẫn\n"
            "  /status — Quota DeepL & thông tin bot\n\n"
            f"{EMOJI['warning']} Giới hạn: {MAX_TEXT_LENGTH:,} ký tự mỗi lần."
        )

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.chat.send_action("typing")
        usage = await self.translator.get_usage()

        if "error" in usage:
            await update.message.reply_text(
                f"{EMOJI['error']} Không lấy được quota: {usage['error']}"
            )
            return

        uptime = datetime.utcnow() - self._start_time
        h, rem  = divmod(int(uptime.total_seconds()), 3600)
        m       = rem // 60

        await update.message.reply_text(
            f"{EMOJI['quota']} Trạng thái DeepL:\n\n"
            f"• Đã dùng : {usage['used']:,} ký tự\n"
            f"• Giới hạn: {usage['limit']:,} ký tự\n"
            f"• Còn lại : {usage['remaining']:,} ký tự "
            f"({100 - usage['percent_used']:.1f}%)\n\n"
            f"{EMOJI['cache']} Cache   : {self.translator.cache.size:,} bản dịch\n"
            f"{EMOJI['time']} Uptime  : {h}h {m}m"
        )

    # ── Translation ───────────────────────────
    async def handle_translation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        text = update.message.text.strip()

        if not text:
            await update.message.reply_text(f"{EMOJI['warning']} Vui lòng nhập văn bản.")
            return

        if len(text) > MAX_TEXT_LENGTH:
            await update.message.reply_text(
                f"{EMOJI['warning']} Văn bản quá dài ({len(text):,} ký tự).\n"
                f"Giới hạn: {MAX_TEXT_LENGTH:,} ký tự."
            )
            return

        await update.message.chat.send_action("typing")

        try:
            result = await self.translator.translate(text, target_lang="JA")

            if result.success and result.translated_text:
                suffix = f" {EMOJI['cache']}" if result.from_cache else ""
                await update.message.reply_text(result.translated_text + suffix)
                logger.info(
                    f"User {user.id} | {result.detected_source_lang} → JA"
                    + (" (cache)" if result.from_cache else "")
                )
            else:
                msg = result.error_message or "Lỗi dịch thuật không xác định."
                await update.message.reply_text(f"{EMOJI['error']} {msg}")
                logger.warning(f"Dịch thất bại user {user.id}: {msg}")

        except Exception as e:
            logger.error(f"Lỗi xử lý user {user.id}: {e}", exc_info=True)
            await update.message.reply_text(f"{EMOJI['error']} Đã xảy ra lỗi, vui lòng thử lại.")

    async def button_callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.callback_query.answer("Vui lòng gửi văn bản trực tiếp.", show_alert=True)


# ─────────────────────────────────────────────
# FastAPI + Lifespan
# ─────────────────────────────────────────────
bot: Optional[TelegramBot]       = None
bot_init_task: Optional[asyncio.Task] = None
keepalive: Optional[KeepAliveService] = None
is_bot_ready = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot, bot_init_task, keepalive, is_bot_ready

    token        = os.environ.get("TELEGRAM_BOT_TOKEN")
    webhook_url  = os.environ.get("WEBHOOK_URL", "").rstrip("/")
    deepl_key    = os.environ.get("DEEPL_API_KEY")

    missing = [k for k, v in {
        "TELEGRAM_BOT_TOKEN": token,
        "WEBHOOK_URL":        webhook_url,
        "DEEPL_API_KEY":      deepl_key,
    }.items() if not v]
    if missing:
        raise RuntimeError(f"Thiếu biến môi trường: {', '.join(missing)}")

    # ── Tạo HTTP session ─────────────────────
    connector = aiohttp.TCPConnector(
        limit=100,
        ttl_dns_cache=300,
        use_dns_cache=True,
    )
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=15, connect=5),
        connector=connector,
    )

    try:
        # ── Khởi tạo bot (background, không block startup) ──
        bot = TelegramBot(deepl_api_key=deepl_key)
        bot.translator.set_session(session)
        bot_init_task = asyncio.create_task(
            bot.initialize(token, webhook_url),
            name="bot_init",
        )
        is_bot_ready = True

        # ── Khởi tạo keep-alive ──────────────
        keepalive = KeepAliveService(
            ping_url=f"{webhook_url}/ping",
            session=session,
        )
        keepalive.start()

        yield

    finally:
        # ── Dọn dẹp khi shutdown ─────────────
        if keepalive:
            await keepalive.stop()

        if bot_init_task and not bot_init_task.done():
            bot_init_task.cancel()
            try:
                await bot_init_task
            except asyncio.CancelledError:
                pass

        if bot and bot.application:
            await bot.application.shutdown()

        if not session.closed:
            await session.close()


app = FastAPI(
    title="Telegram Translation Bot",
    version=VERSION,
    lifespan=lifespan,
    docs_url=None if ENVIRONMENT == "production" else "/docs",
    redoc_url=None if ENVIRONMENT == "production" else "/redoc",
)


# ─────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────

@app.get("/ping")
async def ping():
    """
    Endpoint siêu nhẹ — chỉ dùng để keep-alive.
    Trả về ngay, không cần bot sẵn sàng.
    """
    return {"ok": True, "ts": datetime.utcnow().isoformat()}


@app.get("/")
async def root():
    if not bot:
        return {"status": "initializing", "version": VERSION}
    uptime = (datetime.utcnow() - bot._start_time).total_seconds()
    return {
        "status":         "active" if bot._initialized else "starting",
        "version":        VERSION,
        "uptime_seconds": uptime,
        "bot_ready":      bot._initialized,
    }


@app.get("/health")
async def health():
    if not is_bot_ready or not bot:
        raise HTTPException(status_code=503, detail="Đang khởi tạo")

    bot_status  = await bot.get_bot_status()
    trans_status = await bot.translator.get_service_status()
    overall = (
        "ok" if bot_status["status"] == "active"
             and trans_status["status"] == "active"
        else "degraded"
    )

    return {
        "status":    overall,
        "version":   VERSION,
        "keepalive": keepalive.stats if keepalive else None,
        "services":  {"bot": bot_status, "translation": trans_status},
    }


@app.post("/{token}")
async def telegram_webhook(token: str, request: Request):
    """
    Xử lý webhook từ Telegram.

    Nếu bot chưa init xong (cold start), chờ tối đa BOT_INIT_TIMEOUT giây
    rồi mới xử lý — tránh Telegram retry quá sớm hoặc bỏ mất tin nhắn.
    """
    if token != os.environ.get("TELEGRAM_BOT_TOKEN"):
        raise HTTPException(status_code=403, detail="Token không hợp lệ")

    # Đọc body sớm, trước khi chờ
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="JSON không hợp lệ")

    # Chờ bot init xong (xử lý cold start)
    waited = 0
    while not (bot and bot._initialized) and waited < BOT_INIT_TIMEOUT:
        await asyncio.sleep(1)
        waited += 1

    if not (bot and bot._initialized):
        # Vẫn chưa xong — trả 200 để Telegram không retry ngay,
        # Telegram sẽ tự gửi lại sau ~1 phút
        logger.warning(f"Webhook nhận lúc bot chưa sẵn sàng (đã chờ {waited}s)")
        return JSONResponse({"status": "ok", "note": "bot_starting"})

    try:
        update = Update.de_json(body, bot.application.bot)
        await bot.application.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error")


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    uvicorn.Server(
        uvicorn.Config(
            "bot:app",
            host="0.0.0.0",
            port=int(os.environ.get("PORT", 10000)),
            workers=1,          # 1 worker để giữ state in-memory nhất quán
            loop="uvloop",
            http="httptools",
            log_level="info",
            reload=False,
            access_log=False,
            proxy_headers=True,
            forwarded_allow_ips="*",
        )
    ).run()
