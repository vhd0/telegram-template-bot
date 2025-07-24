import asyncio
import logging
import os
import json
import aiohttp
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

# Khởi tạo FastAPI app
app = FastAPI()

# Biến global cho application và session
application: Application = None
http_session: aiohttp.ClientSession = None

# Các API translate dự phòng
TRANSLATION_APIS = [
    {
        "url": "https://libretranslate.de/translate",
        "payload": lambda text: {
            "q": text,
            "source": "auto",
            "target": "ja",
            "format": "text"
        }
    },
    {
        "url": "https://translate.argosopentech.com/translate",
        "payload": lambda text: {
            "q": text,
            "source": "auto",
            "target": "ja",
            "format": "text"
        }
    }
]

async def try_translate_with_api(session: aiohttp.ClientSession, text: str, api_config: dict) -> str:
    """Try to translate text using a specific API."""
    try:
        async with session.post(
            api_config["url"],
            json=api_config["payload"](text),
            timeout=aiohttp.ClientTimeout(total=10)
        ) as response:
            if response.status == 200:
                data = await response.json()
                if "translatedText" in data:
                    return data["translatedText"]
            logger.warning(f"API {api_config['url']} returned status {response.status}")
            return None
    except Exception as e:
        logger.error(f"Error with API {api_config['url']}: {str(e)}")
        return None

async def translate_text_with_fallback(text: str, max_retries: int = 2) -> str:
    """Attempt to translate text using multiple APIs with retry logic."""
    global http_session
    
    if not http_session or http_session.closed:
        http_session = aiohttp.ClientSession()

    for attempt in range(max_retries):
        for api_config in TRANSLATION_APIS:
            try:
                result = await try_translate_with_api(http_session, text, api_config)
                if result:
                    return result
            except Exception as e:
                logger.error(f"Translation attempt {attempt + 1} failed for API {api_config['url']}: {str(e)}")
                continue
        
        if attempt < max_retries - 1:
            await asyncio.sleep(1)
    
    return None

# Command handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    user = update.effective_user
    logger.info(f"User {user.id} started the bot")
    welcome_text = (
        f"Xin chào {user.first_name}!\n\n"
        "Bot này sẽ dịch văn bản bạn gửi sang tiếng Nhật.\n"
        "Bạn chỉ cần gửi tin nhắn, bot sẽ trả về bản dịch.\n"
        "Gõ /help nếu cần trợ giúp."
    )
    await update.message.reply_text(welcome_text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""
    logger.info(f"User {update.effective_user.id} requested help")
    help_text = (
        "Hướng dẫn sử dụng:\n\n"
        "1. Gửi bất kỳ văn bản nào, bot sẽ dịch sang tiếng Nhật\n"
        "2. /start - Xem lời chào\n"
        "3. /help - Xem hướng dẫn này\n\n"
        "Lưu ý: Độ dài văn bản không quá 500 ký tự"
    )
    await update.message.reply_text(help_text)

async def translate_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Translate text messages to Japanese."""
    user = update.effective_user
    text = update.message.text.strip()
    
    if not text:
        await update.message.reply_text("⚠️ Vui lòng nhập văn bản để dịch.")
        return

    if len(text) > 500:
        await update.message.reply_text("⚠️ Văn bản quá dài. Vui lòng giữ dưới 500 ký tự.")
        return

    logger.info(f"Translating for user {user.id}: {text[:50]}...")
    
    try:
        # Hiển thị đang typing
        await update.message.chat.send_action("typing")
        
        # Thực hiện dịch
        translated = await translate_text_with_fallback(text)
        
        if translated:
            response_text = (
                f"🔄 Bản dịch:\n"
                f"{translated}\n\n"
                f"📝 Văn bản gốc:\n"
                f"{text}"
            )
            logger.info(f"Translation successful for user {user.id}")
        else:
            response_text = "⚠️ Có lỗi xảy ra khi dịch văn bản. Vui lòng thử lại sau."
            logger.error(f"Translation failed for user {user.id}")
        
        await update.message.reply_text(response_text)
        
    except Exception as e:
        logger.error(f"Error while translating for user {user.id}: {str(e)}")
        await update.message.reply_text("⚠️ Có lỗi xảy ra. Vui lòng thử lại sau.")

async def setup_application() -> Application:
    """Setup and initialize the application."""
    global application
    
    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    WEBHOOK_URL = os.getenv("WEBHOOK_URL")
    
    if not TOKEN or not WEBHOOK_URL:
        raise ValueError("Missing required environment variables")

    try:
        application = (
            ApplicationBuilder()
            .token(TOKEN)
            .build()
        )
        
        # Đăng ký handlers
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, translate_text))
        
        # Khởi tạo application và webhook
        await application.initialize()
        await application.bot.set_webhook(url=f"{WEBHOOK_URL}/{TOKEN}")
        logger.info(f"Webhook set up at {WEBHOOK_URL}/{TOKEN}")
        
        return application
    except Exception as e:
        logger.error(f"Error during application setup: {str(e)}")
        raise

# Startup event
@app.on_event("startup")
async def startup_event():
    """Initialize the application on startup."""
    try:
        await setup_application()
        logger.info("Application successfully initialized")
    except Exception as e:
        logger.error(f"Failed to initialize application: {str(e)}")
        raise

# Shutdown event
@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown."""
    global application, http_session
    if application:
        await application.shutdown()
        logger.info("Application shutdown complete")
    
    if http_session and not http_session.closed:
        await http_session.close()
        logger.info("HTTP session closed")

# FastAPI routes
@app.get("/")
async def root():
    """Root endpoint for health check."""
    return {"status": "active", "timestamp": asyncio.get_event_loop().time()}

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok"}

@app.post(f"/{{token}}")
async def telegram_webhook(token: str, request: Request):
    """Webhook endpoint for Telegram updates."""
    global application
    
    if token != os.getenv("TELEGRAM_BOT_TOKEN"):
        logger.warning(f"Invalid token received: {token[:10]}...")
        raise HTTPException(status_code=403, detail="Invalid token")
    
    if not application:
        logger.error("Application not initialized")
        raise HTTPException(status_code=500, detail="Application not initialized")
    
    try:
        update_data = await request.json()
        update = Update.de_json(update_data, application.bot)
        await application.process_update(update)
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
