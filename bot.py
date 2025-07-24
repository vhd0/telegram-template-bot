import asyncio
import logging
import os
import requests
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes
)
from contextlib import asynccontextmanager

# Cấu hình logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Khởi tạo FastAPI app
app = FastAPI()

# Biến global cho application
application = None

# Hàm gọi LibreTranslate API với retry logic
async def libre_translate(text: str, source: str = 'auto', target: str = 'ja', max_retries: int = 3) -> str:
    url = "https://libretranslate.de/translate"
    payload = {
        "q": text,
        "source": source,
        "target": target,
        "format": "text"
    }
    
    for attempt in range(max_retries):
        try:
            async with asyncio.timeout(10):  # 10 seconds timeout
                response = requests.post(url, json=payload)
                response.raise_for_status()
                result = response.json()
                return result.get('translatedText', '')
        except Exception as e:
            logger.error(f"Translation attempt {attempt + 1} failed: {str(e)}")
            if attempt == max_retries - 1:  # Last attempt
                logger.error(f"All translation attempts failed for text: {text}")
                return None
            await asyncio.sleep(1)  # Wait 1 second before retrying

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
        translated = await libre_translate(text)
        
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

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan manager for FastAPI application."""
    global application
    
    # Lấy environment variables
    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    WEBHOOK_URL = os.getenv("WEBHOOK_URL")
    
    if not TOKEN or not WEBHOOK_URL:
        raise ValueError("Missing required environment variables")

    try:
        # Khởi tạo application
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
        
        yield
        
    except Exception as e:
        logger.error(f"Error during startup: {str(e)}")
        raise
    finally:
        # Cleanup
        if application:
            await application.shutdown()
            logger.info("Application shutdown complete")

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
        return {"status": "error", "message": "Invalid token"}
    
    try:
        update = Update.de_json(await request.json(), application.bot)
        await application.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error processing update: {str(e)}")
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    # Chạy với uvicorn khi chạy trực tiếp
    import uvicorn
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run(
        "bot:app",
        host="0.0.0.0",
        port=port,
        workers=1,
        reload=False
    )
